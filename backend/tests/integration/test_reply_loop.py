"""Сквозной сценарий MVP (ТЗ §45).

Сообщение → правило → AI-анализ → контекст → генерация → валидация →
cooldown → отправка → запись в базу → realtime-событие.

Telegram и модель подменены; всё остальное настоящее: PostgreSQL, Redis,
правила, валидатор, движок действий. Именно здесь проверяется, что система
не отвечает дважды, не отвечает сама себе и не молчит без записи причины.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
from redis.asyncio import Redis
from sqlalchemy import delete, func, select

from app.actions.cooldown import CooldownGuard
from app.actions.engine import ActionEngine
from app.actions.handlers import (
    EscalateToHumanHandler,
    IgnoreHandler,
    NotifyAdminHandler,
    ReplyHandler,
    SaveLeadHandler,
    TagUserHandler,
)
from app.actions.validator import ReplyValidator
from app.ai.analyzer import AIAnalyzer
from app.ai.budget import AIBudget, UsageRecorder
from app.ai.generator import AIGenerator
from app.ai.provider import AnalysisResult, GeneratedReply, Intent
from app.bus.messages import Event
from app.conversations.context import ContextBuilder
from app.core.config import Settings
from app.database.session import Database
from app.models import (
    Account,
    AccountStatus,
    Action,
    ActionStatus,
    ActionType,
    AIRequest,
    Chat,
    ChatType,
    Message,
    Notification,
    ProcessedMessage,
    ProcessedStatus,
    Rule,
    RuleScope,
    Scenario,
)
from app.pipeline.monitor_pipeline import MonitorPipeline
from app.pipeline.reply_pipeline import ReplyPipeline
from app.rules.engine import RuleEngine
from app.rules.filters import SelfGuard
from app.telegram.client_manager import ClientManager
from app.telegram.sender import MessageSender
from app.telegram.session_manager import SessionCredentials
from tests.builders import FakeEvent
from tests.fakes import (
    FakeAIProvider,
    FakeClientFactory,
    FakeTelegramClient,
    make_session_string,
)

pytestmark = pytest.mark.integration


@dataclass
class RecordingPublisher:
    events: list[Event] = field(default_factory=list)

    async def publish(self, event: Event) -> None:
        self.events.append(event)


@dataclass
class Env:
    pipeline: MonitorPipeline
    database: Database
    provider: FakeAIProvider
    client: FakeTelegramClient
    publisher: RecordingPublisher
    account_id: uuid.UUID
    chat_id: uuid.UUID
    tg_chat_id: int
    rule_id: uuid.UUID
    scenario_id: uuid.UUID
    clients: ClientManager


@pytest.fixture
async def env(integration_settings: Settings, redis_client: Redis) -> AsyncIterator[Env]:
    settings = integration_settings.model_copy(
        update={
            "send_min_interval_seconds": 0.0,
            "max_consecutive_ai_replies": 3,
            "reply_typing_delay_min_seconds": 0.0,
            "reply_typing_delay_max_seconds": 0.0,
        }
    )
    database = Database(settings)
    await database.connect()

    tg_chat_id = -100_000_000 - (uuid.uuid4().int % 1_000_000)
    tg_user_id = uuid.uuid4().int % 1_000_000_000
    suffix = uuid.uuid4().hex[:8]

    async with database.session() as db:
        account = Account(label="reply-loop", tg_user_id=tg_user_id, status=AccountStatus.ONLINE)
        scenario = Scenario(
            name=f"Продажи {suffix}",
            system_prompt="Ты менеджер дизайн-студии. Отвечай коротко и по делу.",
            max_reply_length=500,
            context_messages=10,
        )
        db.add_all([account, scenario])
        await db.flush()

        chat = Chat(
            account_id=account.id,
            tg_chat_id=tg_chat_id,
            type=ChatType.SUPERGROUP,
            title="Фриланс-чат",
            monitored=True,
        )
        rule = Rule(
            name=f"Поиск клиентов {suffix}",
            enabled=True,
            # Заведомо выше любых правил, созданных оператором вручную:
            # тест не должен зависеть от содержимого рабочей базы.
            priority=10_000,
            scope=RuleScope.CHAT_MONITOR,
            scenario_id=scenario.id,
            keywords={"terms": ["нужен дизайнер"], "mode": "substring"},
            ai_enabled=True,
            ai_threshold=0.8,
            cooldown={"user": 600},
            action=ActionType.REPLY,
        )
        db.add_all([chat, rule])
        await db.flush()
        account_id, chat_id, rule_id, scenario_id = account.id, chat.id, rule.id, scenario.id

    provider = FakeAIProvider()
    publisher = RecordingPublisher()
    client = FakeTelegramClient()
    factory = FakeClientFactory()
    factory.preset(account_id, client)

    clients = ClientManager(settings, factory)
    await clients.start(account_id, SessionCredentials(make_session_string(), 1, "hash"))
    await clients.wait_ready(account_id, timeout_seconds=3.0)

    budget = AIBudget(settings, redis_client)
    recorder = UsageRecorder(settings)
    actions = ActionEngine(database)
    actions.register(
        ActionType.REPLY,
        ReplyHandler(database, clients, MessageSender(settings), publisher),  # type: ignore[arg-type]
    )
    actions.register(ActionType.NOTIFY_ADMIN, NotifyAdminHandler(database, publisher))  # type: ignore[arg-type]
    actions.register(ActionType.SAVE_LEAD, SaveLeadHandler(database, publisher))  # type: ignore[arg-type]
    actions.register(
        ActionType.ESCALATE_TO_HUMAN,
        EscalateToHumanHandler(database, publisher),  # type: ignore[arg-type]
    )
    actions.register(ActionType.TAG_USER, TagUserHandler(database))
    actions.register(ActionType.IGNORE, IgnoreHandler(database))

    reply = ReplyPipeline(
        settings,
        database,
        analyzer=AIAnalyzer(provider, budget, recorder, database),  # type: ignore[arg-type]
        generator=AIGenerator(provider, budget, recorder, database),  # type: ignore[arg-type]
        context=ContextBuilder(settings, database),
        validator=ReplyValidator(),
        cooldown=CooldownGuard(redis_client),
        actions=actions,
    )
    pipeline = MonitorPipeline(
        settings,
        database,
        RuleEngine(database, default_user_cooldown=600, cache_ttl_seconds=0.0),
        SelfGuard(own_ids=[tg_user_id]),
        publisher,  # type: ignore[arg-type]
        reply_pipeline=reply,
    )

    yield Env(
        pipeline=pipeline,
        database=database,
        provider=provider,
        client=client,
        publisher=publisher,
        account_id=account_id,
        chat_id=chat_id,
        tg_chat_id=tg_chat_id,
        rule_id=rule_id,
        scenario_id=scenario_id,
        clients=clients,
    )

    await clients.shutdown()
    async with database.session() as db:
        await db.execute(delete(Action).where(Action.account_id == account_id))
        await db.execute(delete(AIRequest).where(AIRequest.account_id == account_id))
        await db.execute(delete(Rule).where(Rule.id == rule_id))
        await db.execute(delete(ProcessedMessage).where(ProcessedMessage.account_id == account_id))
        await db.execute(delete(Account).where(Account.id == account_id))
        await db.execute(delete(Scenario).where(Scenario.id == scenario_id))
        await db.execute(delete(Notification).where(Notification.user_id.is_(None)))
    await database.disconnect()


async def incoming(env: Env, text: str = "Всем привет, нужен дизайнер", **kwargs: object):
    return await env.pipeline.handle_event(
        env.account_id,
        FakeEvent(chat_id=env.tg_chat_id, text=text, **kwargs),  # type: ignore[arg-type]
    )


async def actions_of(env: Env, action_type: ActionType) -> list[Action]:
    async with env.database.session() as db:
        rows = await db.scalars(
            select(Action).where(Action.account_id == env.account_id, Action.type == action_type)
        )
        return list(rows.all())


class TestFullLoop:
    async def test_message_leads_to_a_sent_reply(self, env: Env) -> None:
        outcome = await incoming(env)

        assert outcome.reply is not None
        assert outcome.reply.action is ActionType.REPLY
        assert outcome.reply.status is ActionStatus.SENT
        assert outcome.status is ProcessedStatus.REPLIED

        assert len(env.client.sent) == 1
        chat_id, text, reply_to = env.client.sent[0]
        assert chat_id == env.tg_chat_id
        assert "дизайн" in text.lower()
        assert reply_to == 555

    async def test_ai_was_asked_before_answering(self, env: Env) -> None:
        await incoming(env)

        assert env.provider.analyze_calls, "анализ должен предшествовать генерации"
        assert env.provider.generate_calls, "ответ должен быть сгенерирован"

    async def test_reply_is_stored_as_our_own_message(self, env: Env) -> None:
        """Иначе собственный ответ вернётся в конвейер и вызовет новый круг."""
        await incoming(env)

        async with env.database.session() as db:
            reply_row = await db.scalar(
                select(Message).where(
                    Message.account_id == env.account_id, Message.is_bot_reply.is_(True)
                )
            )
        assert reply_row is not None
        assert reply_row.is_outgoing is True
        assert reply_row.processed_status is ProcessedStatus.REPLIED

    async def test_action_is_recorded_with_validation_details(self, env: Env) -> None:
        await incoming(env)

        sent = await actions_of(env, ActionType.REPLY)
        assert len(sent) == 1
        assert sent[0].status is ActionStatus.SENT
        assert sent[0].sent_tg_message_id is not None
        assert sent[0].validation is not None
        assert sent[0].validation["passed"] is True

    async def test_ai_usage_is_accounted(self, env: Env) -> None:
        await incoming(env)

        async with env.database.session() as db:
            count = await db.scalar(
                select(func.count())
                .select_from(AIRequest)
                .where(AIRequest.account_id == env.account_id)
            )
        assert count == 2, "учитываются и анализ, и генерация"

    async def test_realtime_events_reach_the_panel(self, env: Env) -> None:
        await incoming(env)

        types = [event.type.value for event in env.publisher.events]
        assert "message.new" in types
        assert "action.sent" in types


class TestNoDoubleReply:
    async def test_same_message_twice_sends_one_reply(self, env: Env) -> None:
        event = FakeEvent(chat_id=env.tg_chat_id, message_id=7001, text="нужен дизайнер")

        await env.pipeline.handle_event(env.account_id, event)
        await env.pipeline.handle_event(env.account_id, event)

        assert len(env.client.sent) == 1

    async def test_cooldown_blocks_the_second_message_of_the_same_user(self, env: Env) -> None:
        await incoming(env, message_id=7101)
        second = await incoming(env, message_id=7102)

        assert len(env.client.sent) == 1
        assert second.reply is not None
        assert second.reply.action is ActionType.IGNORE
        assert "cooldown" in (second.reply.reason or "")

    async def test_blocked_attempt_is_recorded_not_silent(self, env: Env) -> None:
        """Оператор должен видеть, почему система промолчала."""
        await incoming(env, message_id=7201)
        await incoming(env, message_id=7202)

        ignored = await actions_of(env, ActionType.IGNORE)
        assert ignored
        assert ignored[0].payload.get("reason", "").startswith("cooldown")


class TestAIDecisions:
    async def test_low_confidence_goes_to_a_human(self, env: Env) -> None:
        env.provider.analysis = AnalysisResult(
            relevant=True,
            confidence=0.4,
            intent=Intent.SERVICE_REQUEST,
            should_reply=True,
            reason="сомнительно",
        )

        outcome = await incoming(env)

        assert env.client.sent == []
        assert outcome.reply is not None
        assert outcome.reply.action is ActionType.ESCALATE_TO_HUMAN

    async def test_irrelevant_message_is_ignored_without_generation(self, env: Env) -> None:
        env.provider.analysis = AnalysisResult(
            relevant=False, confidence=0.9, intent=Intent.SPAM, should_reply=False
        )

        outcome = await incoming(env)

        assert env.provider.generate_calls == [], "генерация не нужна, если отвечать не надо"
        assert env.client.sent == []
        assert outcome.reply is not None
        assert outcome.reply.action is ActionType.IGNORE

    async def test_human_request_goes_to_a_human(self, env: Env) -> None:
        env.provider.analysis = AnalysisResult(
            relevant=True,
            confidence=0.99,
            intent=Intent.HUMAN_REQUEST,
            should_reply=False,
            needs_human=True,
            reason="просит менеджера",
        )

        outcome = await incoming(env)

        assert outcome.reply is not None
        assert outcome.reply.action is ActionType.ESCALATE_TO_HUMAN
        assert env.client.sent == []

        async with env.database.session() as db:
            note = await db.scalar(
                select(Notification).where(Notification.title == "Требует внимания")
            )
        assert note is not None, "оператор должен увидеть диалог в панели"

    async def test_unavailable_model_does_not_send_anything(self, env: Env) -> None:
        from app.core.errors import AIError

        env.provider.fail_analyze = AIError("provider is down")

        outcome = await incoming(env)

        assert env.client.sent == []
        assert outcome.reply is not None
        assert outcome.reply.action is ActionType.ESCALATE_TO_HUMAN


class TestValidation:
    async def test_reply_failing_validation_is_not_sent(self, env: Env) -> None:
        env.provider.reply = GeneratedReply(text="Здравствуйте, {name}!")

        outcome = await incoming(env)

        assert env.client.sent == []
        assert outcome.reply is not None
        assert outcome.reply.action is ActionType.ESCALATE_TO_HUMAN
        assert outcome.reply.validation is not None
        assert outcome.reply.validation.passed is False

    async def test_model_refusal_is_escalated(self, env: Env) -> None:
        env.provider.reply = GeneratedReply(
            text="", refused=True, refusal_reason="нет данных о ценах"
        )

        outcome = await incoming(env)

        assert env.client.sent == []
        assert outcome.reply is not None
        assert outcome.reply.action is ActionType.ESCALATE_TO_HUMAN
        assert "цен" in (outcome.reply.reason or "")

    async def test_cooldown_is_released_when_nothing_was_sent(self, env: Env) -> None:
        """Несостоявшийся ответ не должен запирать следующую попытку."""
        env.provider.reply = GeneratedReply(text="Здравствуйте, {name}!")
        await incoming(env, message_id=7301)

        env.provider.reply = GeneratedReply(text="Добрый день! Готовы помочь с дизайном.")
        second = await incoming(env, message_id=7302)

        assert second.reply is not None
        assert second.reply.action is ActionType.REPLY
        assert second.reply.status is ActionStatus.SENT
        assert len(env.client.sent) == 1
