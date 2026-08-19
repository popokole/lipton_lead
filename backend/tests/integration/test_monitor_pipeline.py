"""Конвейер обработки сообщений на живой базе (ТЗ §6, §45).

Здесь проверяется то, что нельзя проверить на моках: сообщение не
обрабатывается дважды, чужой чат не попадает в базу, собственный ответ не
возвращается в конвейер. Эти гарантии держатся на ограничениях PostgreSQL,
поэтому и проверять их нужно на PostgreSQL.

Конвейер открывает свои собственные транзакции, поэтому тесты работают с
реально закоммиченными данными и убирают их за собой сами.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
from sqlalchemy import delete, func, select

from app.bus.messages import Event
from app.core.config import Settings
from app.database.session import Database
from app.models import (
    Account,
    AccountStatus,
    Chat,
    ChatType,
    EventLog,
    EventType,
    Message,
    ProcessedMessage,
    ProcessedStatus,
    Rule,
    RuleScope,
)
from app.pipeline.monitor_pipeline import MonitorPipeline
from app.rules.engine import RuleEngine
from app.rules.filters import SelfGuard
from tests.builders import FakeEvent

pytestmark = pytest.mark.integration


@dataclass
class RecordingPublisher:
    """Собирает события вместо отправки в Redis."""

    events: list[Event] = field(default_factory=list)

    async def publish(self, event: Event) -> None:
        self.events.append(event)


@dataclass
class PipelineEnv:
    pipeline: MonitorPipeline
    database: Database
    account_id: uuid.UUID
    chat_id: uuid.UUID
    tg_chat_id: int
    rule_id: uuid.UUID
    publisher: RecordingPublisher
    self_guard: SelfGuard


@pytest.fixture
async def env(integration_settings: Settings) -> AsyncIterator[PipelineEnv]:
    database = Database(integration_settings)
    await database.connect()

    tg_chat_id = -100_000_000 - (uuid.uuid4().int % 1_000_000)
    tg_user_id = uuid.uuid4().int % 1_000_000_000

    async with database.session() as db:
        account = Account(label="pipeline-test", tg_user_id=tg_user_id, status=AccountStatus.ONLINE)
        db.add(account)
        await db.flush()

        chat = Chat(
            account_id=account.id,
            tg_chat_id=tg_chat_id,
            type=ChatType.SUPERGROUP,
            title="Тестовый чат",
            monitored=True,
        )
        rule = Rule(
            name=f"Поиск клиентов {uuid.uuid4().hex[:8]}",
            enabled=True,
            # Заведомо выше любых правил, созданных оператором вручную:
            # тест не должен зависеть от содержимого рабочей базы.
            priority=10_000,
            scope=RuleScope.CHAT_MONITOR,
            keywords={"terms": ["нужен дизайнер", "ищу дизайнера"], "mode": "substring"},
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
    )

    yield PipelineEnv(
        pipeline=pipeline,
        database=database,
        account_id=account_id,
        chat_id=chat_id,
        tg_chat_id=tg_chat_id,
        rule_id=rule_id,
        publisher=publisher,
        self_guard=self_guard,
    )

    async with database.session() as db:
        await db.execute(delete(Rule).where(Rule.id == rule_id))
        await db.execute(delete(ProcessedMessage).where(ProcessedMessage.account_id == account_id))
        await db.execute(delete(Account).where(Account.id == account_id))
    await database.disconnect()


async def count_messages(env: PipelineEnv) -> int:
    async with env.database.session() as db:
        return int(
            await db.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.account_id == env.account_id)
            )
            or 0
        )


async def load_message(env: PipelineEnv, message_id: uuid.UUID) -> Message | None:
    async with env.database.session() as db:
        return await db.get(Message, message_id)


async def event_types(env: PipelineEnv) -> list[EventType]:
    async with env.database.session() as db:
        rows = await db.scalars(
            select(EventLog.event_type).where(EventLog.account_id == env.account_id)
        )
        return list(rows.all())


class TestHappyPath:
    async def test_matching_message_is_stored_and_marked(self, env: PipelineEnv) -> None:
        outcome = await env.pipeline.handle_event(
            env.account_id,
            FakeEvent(chat_id=env.tg_chat_id, text="Всем привет, нужен дизайнер"),
        )

        assert outcome.status is ProcessedStatus.MATCHED
        assert outcome.message_id is not None
        assert outcome.matched is True
        assert outcome.primary_rule is not None
        assert outcome.primary_rule.rule.id == env.rule_id
        assert outcome.primary_rule.matched_terms == ("нужен дизайнер",)

        stored = await load_message(env, outcome.message_id)
        assert stored is not None
        assert stored.processed_status is ProcessedStatus.MATCHED
        assert stored.rule_id == env.rule_id
        assert stored.chat_id == env.chat_id
        assert stored.is_incoming is True
        assert stored.text == "Всем привет, нужен дизайнер"

    async def test_non_matching_message_is_stored_as_skipped(self, env: PipelineEnv) -> None:
        outcome = await env.pipeline.handle_event(
            env.account_id, FakeEvent(chat_id=env.tg_chat_id, text="обычная болтовня")
        )

        assert outcome.status is ProcessedStatus.SKIPPED
        assert outcome.reason == "no rule matched"
        assert outcome.message_id is not None

        stored = await load_message(env, outcome.message_id)
        assert stored is not None
        assert stored.processed_status is ProcessedStatus.SKIPPED
        assert stored.rule_id is None

    async def test_journal_records_the_path_of_the_message(self, env: PipelineEnv) -> None:
        await env.pipeline.handle_event(
            env.account_id, FakeEvent(chat_id=env.tg_chat_id, text="нужен дизайнер")
        )

        recorded = await event_types(env)
        assert EventType.MESSAGE_RECEIVED in recorded
        assert EventType.RULE_MATCH in recorded

    async def test_realtime_event_is_published(self, env: PipelineEnv) -> None:
        await env.pipeline.handle_event(
            env.account_id, FakeEvent(chat_id=env.tg_chat_id, text="нужен дизайнер")
        )

        assert len(env.publisher.events) == 1
        published = env.publisher.events[0]
        assert published.account_id == env.account_id
        assert published.payload["tg_chat_id"] == env.tg_chat_id


class TestDeduplication:
    async def test_same_event_twice_produces_one_message(self, env: PipelineEnv) -> None:
        """Telethon переспрашивает историю при переподключении — это норма."""
        event = FakeEvent(chat_id=env.tg_chat_id, message_id=9001, text="нужен дизайнер")

        first = await env.pipeline.handle_event(env.account_id, event)
        second = await env.pipeline.handle_event(env.account_id, event)

        assert first.status is ProcessedStatus.MATCHED
        assert second.status is ProcessedStatus.SKIPPED
        assert second.reason == "already claimed"
        assert second.message_id is None
        assert await count_messages(env) == 1

    async def test_different_messages_are_both_processed(self, env: PipelineEnv) -> None:
        for message_id in (9101, 9102, 9103):
            await env.pipeline.handle_event(
                env.account_id,
                FakeEvent(chat_id=env.tg_chat_id, message_id=message_id, text="нужен дизайнер"),
            )

        assert await count_messages(env) == 3


class TestSelfProtection:
    async def test_own_outgoing_message_is_ignored(self, env: PipelineEnv) -> None:
        outcome = await env.pipeline.handle_event(
            env.account_id,
            FakeEvent(chat_id=env.tg_chat_id, text="нужен дизайнер", out=True),
        )

        assert outcome.status is ProcessedStatus.SKIPPED
        assert outcome.reason == "own outgoing message"
        assert await count_messages(env) == 0

    async def test_message_from_our_other_account_is_ignored(self, env: PipelineEnv) -> None:
        """Ответ на сообщение своего же аккаунта — начало бесконечной петли."""
        own_id = next(iter(env.self_guard.own_ids))
        outcome = await env.pipeline.handle_event(
            env.account_id,
            FakeEvent(chat_id=env.tg_chat_id, text="нужен дизайнер", sender_id=own_id),
        )

        assert outcome.status is ProcessedStatus.SKIPPED
        assert "own accounts" in (outcome.reason or "")
        assert await count_messages(env) == 0


class TestChatScope:
    async def test_message_from_unmonitored_chat_is_not_stored(self, env: PipelineEnv) -> None:
        outcome = await env.pipeline.handle_event(
            env.account_id,
            FakeEvent(chat_id=env.tg_chat_id - 777, text="нужен дизайнер"),
        )

        assert outcome.status is ProcessedStatus.SKIPPED
        assert outcome.reason == "chat is not monitored"
        assert await count_messages(env) == 0

    async def test_unknown_chat_is_registered_for_the_operator(self, env: PipelineEnv) -> None:
        """Новый чат появляется в панели, но следить за ним решает человек."""
        foreign_chat_id = env.tg_chat_id - 888
        await env.pipeline.handle_event(
            env.account_id, FakeEvent(chat_id=foreign_chat_id, text="привет")
        )

        async with env.database.session() as db:
            chat = await db.scalar(
                select(Chat).where(
                    Chat.account_id == env.account_id, Chat.tg_chat_id == foreign_chat_id
                )
            )
        assert chat is not None
        assert chat.monitored is False

    async def test_private_chat_is_monitored_from_the_first_message(self, env: PipelineEnv) -> None:
        private_chat_id = abs(env.tg_chat_id) % 1_000_000
        outcome = await env.pipeline.handle_event(
            env.account_id,
            FakeEvent(
                chat_id=private_chat_id,
                text="нужен дизайнер",
                is_private=True,
                is_group=False,
            ),
        )

        assert outcome.message_id is not None, "личная переписка обрабатывается всегда"


class TestBrokenInput:
    async def test_unparsable_event_is_skipped_without_crashing(self, env: PipelineEnv) -> None:
        event = FakeEvent(chat_id=env.tg_chat_id)
        event.message.date = None  # type: ignore[assignment]

        outcome = await env.pipeline.handle_event(env.account_id, event)

        assert outcome.status is ProcessedStatus.SKIPPED
        assert await count_messages(env) == 0
