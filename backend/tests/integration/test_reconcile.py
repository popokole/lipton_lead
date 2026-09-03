"""ChatReconciler на живой базе (ТЗ §6).

Как и в test_monitor_pipeline.py, здесь проверяется то, что нельзя проверить
на моках: claim() внутри handle_event реально идемпотентен на PostgreSQL, а
не просто выглядит идемпотентным в коде.

Не гоняется в стандартной проверке проекта (`pytest tests/unit -q`) — нужна
отдельная тестовая PostgreSQL (integration_settings), которую этот сеанс не
поднимал: код проверен ruff/mypy и вручную прослежен по вызовам, но этот файл
живьём не исполнялся.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, func, select

from app.bus.messages import Event
from app.core.clock import utcnow
from app.core.config import Settings
from app.database.session import Database
from app.models import (
    Account,
    AccountStatus,
    Chat,
    ChatType,
    Message,
    ProcessedMessage,
    Rule,
    RuleScope,
)
from app.pipeline.monitor_pipeline import MonitorPipeline
from app.pipeline.reconcile import ChatReconciler
from app.rules.engine import RuleEngine
from app.rules.filters import SelfGuard
from app.telegram.client import TelegramClientLike
from app.telegram.peers import PeerCache
from tests.builders import FakeEvent, FakeRawMessage
from tests.fakes import FakeTelegramClient

pytestmark = pytest.mark.integration


@dataclass
class RecordingPublisher:
    events: list[Event] = field(default_factory=list)

    async def publish(self, event: Event) -> None:
        self.events.append(event)


@dataclass
class ClientLookupStub:
    """Минимальный ClientLookup: один аккаунт — один клиент, как в проде."""

    client: FakeTelegramClient
    account_id: uuid.UUID

    def get(self, account_id: uuid.UUID) -> TelegramClientLike | None:
        return self.client if account_id == self.account_id else None


@dataclass
class ReconcileEnv:
    reconciler: ChatReconciler
    pipeline: MonitorPipeline
    database: Database
    client: FakeTelegramClient
    account_id: uuid.UUID
    chat_id: uuid.UUID
    tg_chat_id: int
    rule_id: uuid.UUID


@pytest.fixture
async def env(integration_settings: Settings) -> AsyncIterator[ReconcileEnv]:
    database = Database(integration_settings)
    await database.connect()

    tg_chat_id = -100_000_000 - (uuid.uuid4().int % 1_000_000)
    tg_user_id = uuid.uuid4().int % 1_000_000_000

    async with database.session() as db:
        account = Account(
            label="reconcile-test", tg_user_id=tg_user_id, status=AccountStatus.ONLINE
        )
        db.add(account)
        await db.flush()

        chat = Chat(
            account_id=account.id,
            tg_chat_id=tg_chat_id,
            type=ChatType.SUPERGROUP,
            title="Тестовый чат для довыгрузки",
            monitored=True,
            # _active_chats фильтрует по last_message_at — без него чат
            # никогда не попадёт в выборку "недавно активных".
            last_message_at=utcnow(),
        )
        rule = Rule(
            name=f"Поиск клиентов {uuid.uuid4().hex[:8]}",
            enabled=True,
            priority=10_000,
            scope=RuleScope.CHAT_MONITOR,
            keywords={"terms": ["нужен дизайнер"], "mode": "substring"},
        )
        db.add_all([chat, rule])
        await db.flush()

        account_id, chat_id, rule_id = account.id, chat.id, rule.id

    publisher = RecordingPublisher()
    self_guard = SelfGuard(own_ids=[tg_user_id])
    engine = RuleEngine(database, default_user_cooldown=600, cache_ttl_seconds=0.0)
    pipeline = MonitorPipeline(
        integration_settings,
        database,
        engine,
        self_guard,
        publisher,  # type: ignore[arg-type]
        peers=PeerCache(),
    )
    client = FakeTelegramClient()
    reconciler = ChatReconciler(
        database,
        ClientLookupStub(client, account_id),
        pipeline,
        lookback_messages=30,
        active_within_minutes=30,
    )

    # try/finally: без него исключение из теста прокидывается в генератор
    # прямо на yield и пропускает очистку — упавший тест насовсем оставляет
    # Account/Rule в общей базе.
    try:
        yield ReconcileEnv(
            reconciler=reconciler,
            pipeline=pipeline,
            database=database,
            client=client,
            account_id=account_id,
            chat_id=chat_id,
            tg_chat_id=tg_chat_id,
            rule_id=rule_id,
        )
    finally:
        async with database.session() as db:
            await db.execute(delete(Rule).where(Rule.id == rule_id))
            await db.execute(
                delete(ProcessedMessage).where(ProcessedMessage.account_id == account_id)
            )
            await db.execute(delete(Account).where(Account.id == account_id))
        await database.disconnect()


async def count_messages(env: ReconcileEnv) -> int:
    async with env.database.session() as db:
        return int(
            await db.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.account_id == env.account_id)
            )
            or 0
        )


class TestReconcile:
    async def test_recovers_messages_missing_from_the_database(self, env: ReconcileEnv) -> None:
        """Три сообщения «видела history, но нет в базе» — все три довыгружаются."""
        env.client.history = [
            FakeRawMessage(message_id=901, chat_id=env.tg_chat_id, text="привет"),
            FakeRawMessage(message_id=902, chat_id=env.tg_chat_id, text="нужен дизайнер"),
            FakeRawMessage(message_id=903, chat_id=env.tg_chat_id, text="спасибо"),
        ]

        recovered = await env.reconciler.run(env.account_id)

        assert recovered == 3
        assert await count_messages(env) == 3

    async def test_second_run_is_a_no_op_idempotent(self, env: ReconcileEnv) -> None:
        """claim() — не бутафория: повторный проход по той же истории ничего не досыпает."""
        env.client.history = [
            FakeRawMessage(message_id=910, chat_id=env.tg_chat_id, text="нужен дизайнер"),
        ]

        first = await env.reconciler.run(env.account_id)
        second = await env.reconciler.run(env.account_id)

        assert first == 1
        assert second == 0
        assert await count_messages(env) == 1

    async def test_already_known_messages_are_not_recounted(self, env: ReconcileEnv) -> None:
        """Живой конвейер уже сохранил сообщение — довыгрузка его не задваивает."""
        live_event = FakeEvent(message_id=920, chat_id=env.tg_chat_id, text="нужен дизайнер")
        pipeline_outcome = await env.pipeline.handle_event(env.account_id, live_event)
        assert pipeline_outcome.message_id is not None

        # То же сообщение, но в форме "сырого" Message из iter_messages —
        # именно так довыгрузка увидела бы его повторно в реальности.
        env.client.history = [
            FakeRawMessage(message_id=920, chat_id=env.tg_chat_id, text="нужен дизайнер")
        ]
        recovered = await env.reconciler.run(env.account_id)

        assert recovered == 0
        assert await count_messages(env) == 1

    async def test_processes_oldest_first_so_cooldown_favors_the_earliest_message(
        self, env: ReconcileEnv
    ) -> None:
        """iter_messages отдаёт новое->старое; реконсайлер разворачивает порядок."""
        env.client.history = [
            FakeRawMessage(
                message_id=931,
                chat_id=env.tg_chat_id,
                text="нужен дизайнер",
                date=datetime(2026, 8, 23, 10, 1, tzinfo=UTC),
            ),
            FakeRawMessage(
                message_id=930,
                chat_id=env.tg_chat_id,
                text="нужен дизайнер",
                date=datetime(2026, 8, 23, 10, 0, tzinfo=UTC),
            ),
        ]

        await env.reconciler.run(env.account_id)

        async with env.database.session() as db:
            rows = (
                await db.scalars(
                    select(Message)
                    .where(Message.account_id == env.account_id)
                    .order_by(Message.created_at)
                )
            ).all()
        assert [row.tg_message_id for row in rows] == [930, 931]

    async def test_unknown_account_is_a_no_op(self, env: ReconcileEnv) -> None:
        recovered = await env.reconciler.run(uuid.uuid4())

        assert recovered == 0
