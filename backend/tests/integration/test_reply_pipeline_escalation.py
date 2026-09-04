"""Технический сбой AI-анализа обязан уходить оператору (ТЗ §21).

Регрессия на инцидент: у сценария выключен human_handoff_enabled (не дёргать
оператора по каждому неуверенному случаю), и настоящий сбой анализатора
(например, недоступен провайдер) по ошибке проваливался в ту же ветку и тихо
игнорировался — лид терялся молча вместо передачи оператору. Флаг сценария
должен решать, эскалировать ли СОБСТВЕННО решение модели, а не маскировать
поломку инфраструктуры.

ReplyPipeline открывает свои собственные сессии БД, поэтому тестовые данные
должны быть закоммичены (как в test_monitor_pipeline.py), а не жить в
транзакции с откатом — иначе конвейер их просто не увидит.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from redis.asyncio import Redis
from sqlalchemy import delete

from app.actions.cooldown import CooldownGuard
from app.actions.engine import ActionEngine, ActionRequest, ActionResult
from app.actions.handlers import EscalateToHumanHandler, IgnoreHandler
from app.actions.validator import ReplyValidator
from app.ai.analyzer import AnalysisOutcome
from app.ai.generator import GenerationOutcome
from app.ai.provider import AnalysisResult, GeneratedReply, Intent, Usage
from app.bus.messages import Event
from app.conversations.context import ContextBuilder
from app.core.config import Settings
from app.database.session import Database
from app.models import (
    Account,
    AccountStatus,
    ActionStatus,
    ActionType,
    Chat,
    ChatType,
    Rule,
    RuleScope,
    Scenario,
)
from app.pipeline.reply_pipeline import ReplyOutcome, ReplyPipeline
from app.rules.engine import CompiledRule, CooldownSpec, RuleMatch
from app.rules.filters import MessageFilterSpec
from app.rules.keywords import KeywordSpec
from app.telegram.messages import NormalizedMessage
from tests.conftest import make_settings

pytestmark = pytest.mark.integration


@dataclass
class RecordingPublisher:
    events: list[Event] = field(default_factory=list)

    async def publish(self, event: Event) -> None:
        self.events.append(event)


class _StubAnalyzer:
    """Возвращает заранее заданный исход вместо обращения к провайдеру."""

    def __init__(self, outcome: AnalysisOutcome) -> None:
        self._outcome = outcome

    async def analyze(self, **_kwargs: object) -> AnalysisOutcome:
        return self._outcome


def _failed_outcome(threshold: float = 0.7) -> AnalysisOutcome:
    return AnalysisOutcome(
        result=AnalysisResult(
            relevant=False,
            confidence=0.0,
            intent=Intent.OTHER,
            should_reply=False,
            needs_human=True,
            reason="upstream_unavailable",
        ),
        usage=Usage(),
        threshold=threshold,
        failed=True,
        failure_reason="HTTP 502: Could not validate your refresh token",
    )


def _low_confidence_outcome(threshold: float = 0.7) -> AnalysisOutcome:
    """Модель ответила успешно, но неуверенно — не сбой, а решение модели."""
    return AnalysisOutcome(
        result=AnalysisResult(
            relevant=True,
            confidence=0.2,
            intent=Intent.SERVICE_REQUEST,
            should_reply=True,
            needs_human=False,
            reason="not sure",
        ),
        usage=Usage(),
        threshold=threshold,
        failed=False,
    )


def _confident_outcome(threshold: float = 0.7) -> AnalysisOutcome:
    """Уверенное решение модели — конвейер должен дойти до генерации и отправки."""
    return AnalysisOutcome(
        result=AnalysisResult(
            relevant=True,
            confidence=0.95,
            intent=Intent.SERVICE_REQUEST,
            should_reply=True,
            needs_human=False,
            reason="явный запрос",
        ),
        usage=Usage(),
        threshold=threshold,
        failed=False,
    )


class _StubGenerator:
    """Всегда успешно генерирует ответ — сбой в этих тестах имитирует
    сама отправка (ActionEngine/Telethon), а не AI."""

    async def generate(self, _scenario: object, **_kwargs: object) -> GenerationOutcome:
        return GenerationOutcome(
            reply=GeneratedReply(text="Здравствуйте! Подскажем."), usage=Usage()
        )


class _FailingReplyHandler:
    """Имитирует падение Telethon при отправке (регрессия 2026-09-04:
    TypeNotFoundError на битом TL-объекте где-то в апдейтах)."""

    async def execute(self, request: ActionRequest, action_id: object) -> ActionResult:
        return ActionResult(
            status=ActionStatus.FAILED,
            action_id=action_id,  # type: ignore[arg-type]
            detail="TypeNotFoundError: Could not find a matching Constructor ID",
        )


@dataclass
class Env:
    database: Database
    redis_client: Redis
    account_id: uuid.UUID
    chat_id: uuid.UUID


@pytest.fixture
async def env(integration_settings: Settings, redis_client: Redis) -> AsyncIterator[Env]:
    database = Database(integration_settings)
    await database.connect()

    tg_id = uuid.uuid4().int % 10_000_000_000
    async with database.session() as db:
        account = Account(
            label="reply-pipeline-escalation-test",
            tg_user_id=tg_id,
            status=AccountStatus.ONLINE,
        )
        db.add(account)
        await db.flush()
        chat = Chat(
            account_id=account.id,
            tg_chat_id=-tg_id,
            type=ChatType.GROUP,
            title="Escalation test chat",
            monitored=True,
        )
        db.add(chat)
        await db.flush()
        account_id, chat_id = account.id, chat.id

    # try/finally: без него исключение из теста прокидывается в генератор
    # прямо на yield и пропускает очистку — упавший тест насовсем оставляет
    # Account в общей базе.
    try:
        yield Env(
            database=database, redis_client=redis_client, account_id=account_id, chat_id=chat_id
        )
    finally:
        async with database.session() as db:
            await db.execute(delete(Account).where(Account.id == account_id))
        await database.disconnect()


async def _run(
    env: Env,
    scenario: Scenario,
    outcome: AnalysisOutcome,
    *,
    generator: object | None = None,
    reply_handler: object | None = None,
) -> tuple[ReplyOutcome, RecordingPublisher]:
    publisher = RecordingPublisher()
    actions = ActionEngine(env.database)
    actions.register(
        ActionType.ESCALATE_TO_HUMAN,
        EscalateToHumanHandler(env.database, publisher),  # type: ignore[arg-type]
    )
    actions.register(ActionType.IGNORE, IgnoreHandler(env.database))
    if reply_handler is not None:
        actions.register(ActionType.REPLY, reply_handler)  # type: ignore[arg-type]

    db_rule = Rule(
        name="психолог Тест (integration)",
        enabled=True,
        priority=1000,
        scope=RuleScope.CHAT_MONITOR,
        action=ActionType.REPLY,
        ai_enabled=True,
        ai_threshold=0.7,
    )
    async with env.database.session() as db:
        db.add(scenario)
        await db.flush()
        scenario_id = scenario.id
        db_rule.scenario_id = scenario_id
        db.add(db_rule)
        await db.flush()
        rule_id = db_rule.id

    settings = make_settings()
    pipeline = ReplyPipeline(
        settings,
        env.database,
        analyzer=_StubAnalyzer(outcome),  # type: ignore[arg-type]
        generator=generator,  # type: ignore[arg-type]
        context=ContextBuilder(settings, env.database),
        validator=ReplyValidator(),
        cooldown=CooldownGuard(env.redis_client),
        actions=actions,
    )

    rule = CompiledRule(
        id=rule_id,
        name="психолог Тест (integration)",
        priority=1000,
        stop_on_match=True,
        scope=RuleScope.CHAT_MONITOR,
        scenario_id=scenario_id,
        action=ActionType.REPLY,
        action_config={},
        filters=MessageFilterSpec(),
        keywords=KeywordSpec(),
        regex=None,
        ai_enabled=True,
        ai_threshold=0.7,
        cooldown=CooldownSpec(),
    )
    message = NormalizedMessage(
        account_id=env.account_id,
        tg_chat_id=-1,
        tg_message_id=1,
        chat_type=ChatType.GROUP,
        text="Посоветуйте психолога пожалуйста",
        date=datetime.now(UTC),
        is_incoming=True,
        is_outgoing=False,
        sender_tg_id=999_999_999,
    )

    try:
        result = await pipeline.handle(
            message, RuleMatch(rule=rule), chat_id=env.chat_id, message_id=None
        )
    finally:
        # try/finally: правило и сценарий не должны пережить тест, даже если
        # сам вызов handle() упал — иначе такие строки копятся в рабочей базе.
        async with env.database.session() as db:
            await db.execute(delete(Rule).where(Rule.id == rule_id))
            await db.execute(delete(Scenario).where(Scenario.id == scenario_id))

    return result, publisher


class TestAnalysisFailureEscalates:
    async def test_failed_analysis_escalates_even_with_handoff_disabled(self, env: Env) -> None:
        scenario = Scenario(
            name=f"psy-test-{uuid.uuid4().hex[:8]}",
            system_prompt="ты психолог-ассистент",
            human_handoff_enabled=False,  # именно эта настройка маскировала баг
        )
        outcome, publisher = await _run(env, scenario, _failed_outcome())

        assert outcome.action is ActionType.ESCALATE_TO_HUMAN
        assert "502" in (outcome.reason or "") or "refresh token" in (outcome.reason or "")
        assert any(e.type.value == "human.handoff" for e in publisher.events)

    async def test_low_confidence_without_handoff_is_ignored_not_escalated(
        self, env: Env
    ) -> None:
        """Контрольный случай: настоящее (не сбойное) решение модели по-прежнему
        уважает human_handoff_enabled=False и не заваливает оператора."""
        scenario = Scenario(
            name=f"psy-test-{uuid.uuid4().hex[:8]}",
            system_prompt="ты психолог-ассистент",
            human_handoff_enabled=False,
        )
        outcome, _ = await _run(env, scenario, _low_confidence_outcome())

        assert outcome.action is ActionType.IGNORE


class TestSendFailureEscalates:
    """Регрессия на инцидент 2026-09-04: Telethon уронил RPC отправки ответа
    (TypeNotFoundError на неизвестном TL-объекте), лид был корректно
    проанализирован и сгенерирован ответ — но отправка молча осела статусом
    FAILED, и оператор об этом не узнал (см. app/pipeline/reply_pipeline.py)."""

    async def test_send_failure_escalates_even_with_handoff_disabled(self, env: Env) -> None:
        scenario = Scenario(
            name=f"send-fail-test-{uuid.uuid4().hex[:8]}",
            system_prompt="ты ассистент по VPN",
            human_handoff_enabled=False,
        )
        outcome, publisher = await _run(
            env,
            scenario,
            _confident_outcome(),
            generator=_StubGenerator(),
            reply_handler=_FailingReplyHandler(),
        )

        assert outcome.action is ActionType.ESCALATE_TO_HUMAN
        assert "Constructor" in (outcome.reason or "")
        assert any(e.type.value == "human.handoff" for e in publisher.events)
