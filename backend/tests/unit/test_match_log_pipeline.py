"""Карточка совпадения в конвейере: одна на сообщение, итог — всегда.

MonitorPipeline открывает карточку сразу после совпадения (после claim()) и
дописывает итог handle(); итог отложенного ответа дописывает фоновая задача
ReplyPipeline во всех своих концовках. ReplyHandler свою карточку больше не
шлёт — иначе в лог-чате были бы дубли.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.actions import handlers
from app.actions.engine import ActionEngine, ActionRequest, ActionResult
from app.actions.handlers import ReplyHandler
from app.core.errors import TelegramError
from app.models import ActionStatus, ActionType, ChatType, ProcessedStatus
from app.notifications.match_log import (
    NO_REPLY_PIPELINE_LINE,
    STOPLIST_LINE,
    crash_line,
    outcome_line,
)
from app.notifications.notifier import NotifierBot
from app.pipeline import monitor_pipeline
from app.pipeline.monitor_pipeline import MonitorPipeline
from app.pipeline.reply_pipeline import ReplyOutcome
from app.rules.filters import SelfGuard, StopGuard
from app.telegram.messages import NormalizedMessage
from app.telegram.peers import PeerCache
from app.telegram.sender import SentMessage
from tests.conftest import make_settings
from tests.unit.test_scenario_reply_delay import (
    CHAT_ID,
    REPLY_TEXT,
    SENDER,
    WAIT,
    FakeActions,
    delayed,
    drain,
    make_env,
    make_match,
    make_scenario,
    send,
)

ACCOUNT = uuid.uuid4()


class FakeCard:
    """MatchCard без очереди: запоминает, что в неё дописали."""

    def __init__(self) -> None:
        self.outcomes: list[ReplyOutcome] = []
        self.lines: list[str] = []

    def report(self, outcome: ReplyOutcome) -> None:
        self.outcomes.append(outcome)

    def report_line(self, line: str, *, final: bool = True) -> None:
        self.lines.append(line)


# --- ReplyPipeline: итог отложенного ответа ----------------------------------------
class DmFailingActions(FakeActions):
    async def dispatch(self, request: ActionRequest) -> ActionResult:
        result = await super().dispatch(request)
        if request.type is ActionType.REPLY:
            return dataclasses.replace(result, dm_error="PrivacyRestricted", detail=None)
        return result


class TestReplyPipelineReportsToTheCard:
    async def test_immediate_reply_is_left_to_the_caller(self) -> None:
        env = make_env()
        card = FakeCard()

        sent = await send(env, make_scenario(), card=card)

        assert sent.outcome.replied
        assert sent.outcome.reply_text == REPLY_TEXT
        assert card.outcomes == [], "итог без паузы дописывает MonitorPipeline"

    async def test_reply_in_dm_outcome_carries_both_texts_and_dm_error(self) -> None:
        env = make_env(actions=DmFailingActions())
        scenario = make_scenario(reply_in_dm=True, group_ack_text="Отправлю в лс")

        sent = await send(env, scenario)

        assert sent.outcome.reply_text == "Отправлю в лс"
        assert sent.outcome.dm_text == REPLY_TEXT
        assert sent.outcome.dm_error == "PrivacyRestricted"
        assert "⚠️ Личка не ушла: PrivacyRestricted" in outcome_line(sent.outcome)

    async def test_immediate_send_failure_is_a_send_error(self) -> None:
        env = make_env(actions=FakeActions(reply_status=ActionStatus.FAILED))

        sent = await send(env, make_scenario())

        assert sent.outcome.action is ActionType.ESCALATE_TO_HUMAN
        assert sent.outcome.send_error == "fake"
        assert outcome_line(sent.outcome) == ("❌ Не удалось отправить: fake · передано оператору")

    async def test_delayed_reply_reports_the_final_result_after_the_wait(self) -> None:
        env = make_env()
        card = FakeCard()

        sent = await send(env, delayed(30), card=card)

        assert sent.outcome.scheduled
        assert sent.outcome.delay_seconds == 30
        assert outcome_line(sent.outcome) == "⏳ Ответ запланирован через 30 с"
        assert card.outcomes == [], "«⏳» дописывает вызывающий, итог — задача"

        env.sleep.release.set()
        await drain(env)

        (final,) = card.outcomes
        assert final.replied
        assert final.reply_text == REPLY_TEXT
        assert outcome_line(final).startswith("✅ Ответ отправлен (после паузы 30 с)")

    async def test_reply_cancelled_by_the_operator_during_the_wait(self) -> None:
        env = make_env()
        env.database.add(CHAT_ID, monitored=True)
        card = FakeCard()
        await send(env, delayed(30), chat_id=CHAT_ID, card=card)
        await asyncio.wait_for(env.sleep.started.wait(), timeout=WAIT)

        env.stop_guard.update([SENDER], [])
        env.sleep.release.set()
        await drain(env)

        (final,) = card.outcomes
        assert final.action is ActionType.IGNORE
        assert outcome_line(final) == (
            "⏭ Не отправляли: стоп-лист: отправитель добавлен во время паузы"
        )

    async def test_failed_send_after_the_wait(self) -> None:
        env = make_env(actions=FakeActions(reply_status=ActionStatus.FAILED))
        card = FakeCard()
        await send(env, delayed(1), card=card)

        env.sleep.release.set()
        await drain(env)

        (final,) = card.outcomes
        assert final.action is ActionType.ESCALATE_TO_HUMAN
        assert final.send_error == "fake"

    async def test_crash_during_the_delayed_send(self) -> None:
        async def boom(_request: ActionRequest) -> None:
            raise RuntimeError("db is down")

        env = make_env(actions=FakeActions(on_reply=boom))
        card = FakeCard()
        await send(env, delayed(1), card=card)

        env.sleep.release.set()
        await drain(env)

        (final,) = card.outcomes
        assert final.send_error is not None and "db is down" in final.send_error
        assert outcome_line(final).startswith("❌ Не удалось отправить: отложенный ответ упал")

    async def test_shutdown_during_the_wait(self) -> None:
        env = make_env()
        card = FakeCard()
        await send(env, delayed(600), card=card)
        await asyncio.wait_for(env.sleep.started.wait(), timeout=WAIT)

        await env.pipeline.shutdown(grace_seconds=WAIT)

        (final,) = card.outcomes
        assert final.action is ActionType.ESCALATE_TO_HUMAN
        assert final.send_error is None, "ответа не было — это не сбой отправки"
        assert "остановка воркера" in outcome_line(final)

    async def test_failed_cleanup_still_leaves_a_result(self) -> None:
        async def escalation_down(_request: ActionRequest) -> None:
            raise RuntimeError("db is down")

        env = make_env(actions=FakeActions(on_escalate=escalation_down))
        card = FakeCard()
        await send(env, delayed(600), card=card)
        await asyncio.wait_for(env.sleep.started.wait(), timeout=WAIT)

        await env.pipeline.shutdown(grace_seconds=WAIT)

        assert card.outcomes == []
        (line,) = card.lines
        assert line.startswith("⏭ Не отправляли: отложенный ответ отменён")
        assert "передать оператору не удалось" in line


# --- MonitorPipeline: одна карточка на совпадение ------------------------------------
class FakeReporter:
    def __init__(self) -> None:
        self.cards: list[tuple[NormalizedMessage, Any, FakeCard]] = []

    def open_card(self, message: NormalizedMessage, match: Any) -> FakeCard:
        card = FakeCard()
        self.cards.append((message, match, card))
        return card


class FakeReply:
    def __init__(self, outcome: ReplyOutcome | Exception, reporter: FakeReporter) -> None:
        self._outcome = outcome
        self._reporter = reporter
        self.calls: list[Any] = []
        self.cards_open_at_call: list[int] = []

    async def handle(
        self,
        message: NormalizedMessage,
        match: Any,
        *,
        chat_id: uuid.UUID | None,
        message_id: uuid.UUID | None,
        card: Any = None,
    ) -> ReplyOutcome:
        self.calls.append(card)
        self.cards_open_at_call.append(len(self._reporter.cards))
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


class FakeRules:
    def __init__(self, matches: list[Any]) -> None:
        self.matches = matches
        self.calls = 0

    async def match_all(self, _message: Any, *, chat_id: Any, scope: Any) -> list[Any]:
        self.calls += 1
        return list(self.matches)


class NullDatabase:
    @contextlib.asynccontextmanager
    async def session(self) -> AsyncIterator[Any]:
        yield None


class NullPublisher:
    async def publish(self, _event: Any) -> None:
        return None


@dataclasses.dataclass
class Store:
    monitored: bool = True
    status_error: Exception | None = None
    claimed: set[tuple[Any, ...]] = dataclasses.field(default_factory=set)
    statuses: list[tuple[ProcessedStatus, str | None]] = dataclasses.field(default_factory=list)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Store:
    """Репозитории MonitorPipeline в памяти (claim идемпотентен, как в базе)."""
    state = Store()

    class Messages:
        def __init__(self, _db: Any) -> None:
            pass

        async def claim(self, message: NormalizedMessage) -> bool:
            if message.dedup_key in state.claimed:
                return False
            state.claimed.add(message.dedup_key)
            return True

        async def save(self, _message: Any, *, chat_id: Any) -> Any:
            return SimpleNamespace(id=uuid.uuid4())

        async def set_status(
            self, _message_id: Any, status: ProcessedStatus, **kwargs: Any
        ) -> None:
            if state.status_error is not None:
                raise state.status_error
            state.statuses.append((status, kwargs.get("reason")))

    class Chats:
        def __init__(self, _db: Any) -> None:
            pass

        async def ensure(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(id=CHAT_ID, monitored=state.monitored)

        async def touch(self, _chat_id: Any) -> None:
            return None

    class Events:
        def __init__(self, _db: Any) -> None:
            pass

        async def add(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    monkeypatch.setattr(monitor_pipeline, "MessageRepository", Messages)
    monkeypatch.setattr(monitor_pipeline, "ChatRepository", Chats)
    monkeypatch.setattr(monitor_pipeline, "EventLogRepository", Events)
    return state


def incoming(tg_message_id: int = 7, *, age_seconds: float = 5) -> NormalizedMessage:
    return NormalizedMessage(
        account_id=ACCOUNT,
        tg_chat_id=-100_500,
        tg_message_id=tg_message_id,
        chat_type=ChatType.SUPERGROUP,
        text="нужен дизайнер",
        date=datetime.now(UTC) - timedelta(seconds=age_seconds),
        is_incoming=True,
        is_outgoing=False,
        sender_tg_id=SENDER,
    )


IGNORED = ReplyOutcome(action=ActionType.IGNORE, status=ActionStatus.SENT, reason="cooldown: user")


def make_monitor(
    *,
    matches: list[Any] | None = None,
    outcome: ReplyOutcome | Exception = IGNORED,
    with_reply: bool = True,
    with_log: bool = True,
    stop_guard: StopGuard | None = None,
) -> tuple[MonitorPipeline, FakeReporter, FakeReply, FakeRules]:
    reporter = FakeReporter()
    reply = FakeReply(outcome, reporter)
    rules = FakeRules([make_match()] if matches is None else matches)
    pipeline = MonitorPipeline(
        make_settings(),
        NullDatabase(),  # type: ignore[arg-type]
        rules,  # type: ignore[arg-type]
        SelfGuard(),
        NullPublisher(),  # type: ignore[arg-type]
        reply_pipeline=reply if with_reply else None,  # type: ignore[arg-type]
        stop_guard=stop_guard,
        match_log=reporter if with_log else None,  # type: ignore[arg-type]
    )
    return pipeline, reporter, reply, rules


class TestMonitorPipelineCards:
    async def test_match_opens_one_card_before_the_reply_and_reports_the_outcome(
        self, store: Store
    ) -> None:
        pipeline, reporter, reply, _ = make_monitor()

        result = await pipeline._process(incoming())

        assert result.status is ProcessedStatus.IGNORED
        ((_message, match, card),) = reporter.cards
        assert match.rule.name == "Поиск клиентов"
        assert reply.cards_open_at_call == [1], "карточка открыта до ответа"
        assert reply.calls == [card], "карточка едет в ReplyPipeline"
        assert card.outcomes == [IGNORED]

    async def test_the_same_message_twice_gives_one_card(self, store: Store) -> None:
        """Реконсайлер довыгружает уже виденное — claim() не пускает повтор."""
        pipeline, reporter, _, _ = make_monitor()

        await pipeline._process(incoming())
        again = await pipeline._process(incoming())

        assert again.reason == "already claimed"
        assert len(reporter.cards) == 1

    async def test_scheduled_reply_reports_the_pending_line(self, store: Store) -> None:
        scheduled = ReplyOutcome(
            action=ActionType.REPLY,
            status=ActionStatus.PENDING,
            reason="ответ через 30 с (задержка сценария)",
            delay_seconds=30,
        )
        pipeline, reporter, _, _ = make_monitor(outcome=scheduled)

        await pipeline._process(incoming())

        card = reporter.cards[0][2]
        assert card.outcomes == [scheduled]
        assert store.statuses == [(ProcessedStatus.MATCHED, None)], "итог запишет задача"

    async def test_crash_in_the_reply_pipeline_is_reported_and_re_raised(
        self, store: Store
    ) -> None:
        error = RuntimeError("boom")
        pipeline, reporter, _, _ = make_monitor(outcome=error)

        with pytest.raises(RuntimeError):
            await pipeline._process(incoming())

        assert reporter.cards[0][2].lines == [crash_line(error)]

    async def test_database_failure_before_the_reply_is_reported(self, store: Store) -> None:
        store.status_error = ConnectionError("db is down")
        pipeline, reporter, reply, _ = make_monitor()

        with pytest.raises(ConnectionError):
            await pipeline._process(incoming())

        assert reply.calls == []
        assert reporter.cards[0][2].lines == [crash_line(store.status_error)]

    async def test_no_match_no_card(self, store: Store) -> None:
        pipeline, reporter, reply, _ = make_monitor(matches=[])

        await pipeline._process(incoming())

        assert reporter.cards == []
        assert reply.calls == []

    async def test_stoplisted_sender_gets_a_card_but_no_reply(self, store: Store) -> None:
        stop_guard = StopGuard()
        stop_guard.update([SENDER], [])
        pipeline, reporter, reply, _ = make_monitor(stop_guard=stop_guard)

        result = await pipeline._process(incoming())

        assert result.status is ProcessedStatus.SKIPPED
        assert reply.calls == []
        assert reporter.cards[0][2].lines == [STOPLIST_LINE]

    async def test_stoplisted_sender_in_an_unmonitored_chat_gets_nothing(
        self, store: Store
    ) -> None:
        store.monitored = False
        stop_guard = StopGuard()
        stop_guard.update([SENDER], [])
        pipeline, reporter, _, rules = make_monitor(stop_guard=stop_guard)

        await pipeline._process(incoming())

        assert reporter.cards == []
        assert rules.calls == 0

    async def test_stale_backlog_gets_no_card(self, store: Store) -> None:
        pipeline, reporter, _, rules = make_monitor()

        await pipeline._process(incoming(age_seconds=3600))

        assert reporter.cards == []
        assert rules.calls == 0

    async def test_without_a_reply_pipeline(self, store: Store) -> None:
        pipeline, reporter, _, _ = make_monitor(with_reply=False)

        await pipeline._process(incoming())

        assert reporter.cards[0][2].lines == [NO_REPLY_PIPELINE_LINE]

    async def test_without_a_log_chat(self, store: Store) -> None:
        pipeline, _, reply, _ = make_monitor(with_log=False)

        result = await pipeline._process(incoming())

        assert result.status is ProcessedStatus.IGNORED
        assert reply.calls == [None]


# --- ReplyHandler: больше не шлёт свою карточку ---------------------------------------
class FakeSender:
    def __init__(self, *, fail_dm: bool = False) -> None:
        self.fail_dm = fail_dm
        self.sent: list[tuple[int, str]] = []

    async def send(
        self, _account_id: Any, _client: Any, *, chat_id: int, text: str, **_kwargs: Any
    ) -> SentMessage:
        if self.fail_dm and chat_id == SENDER:
            raise TelegramError("PrivacyRestricted")
        self.sent.append((chat_id, text))
        return SentMessage(tg_message_id=500 + len(self.sent), chat_id=chat_id)


class RecordingDatabase:
    def __init__(self) -> None:
        self.added: list[Any] = []

    @contextlib.asynccontextmanager
    async def session(self) -> AsyncIterator[Any]:
        yield self

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def get(self, _model: Any, _ident: Any) -> Any:
        return None


@pytest.fixture
def lead_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Репозитории ReplyHandler в памяти; Bot API — под запретом."""
    calls: list[dict[str, Any]] = []

    async def no_bot_api(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("ReplyHandler не должен ходить в Bot API")

    class Leads:
        def __init__(self, _db: Any) -> None:
            pass

        async def upsert(self, _account_id: Any, tg_user_id: int, **kwargs: Any) -> Any:
            calls.append({"tg_user_id": tg_user_id, **kwargs})
            return SimpleNamespace(score=kwargs["score"], status=SimpleNamespace(value="HOT"))

    class Quiet:
        def __init__(self, _db: Any) -> None:
            pass

        async def add(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        async def register_reply(self, *_args: Any) -> None:
            return None

    monkeypatch.setattr(NotifierBot, "_call", no_bot_api)
    monkeypatch.setattr(handlers, "LeadRepository", Leads)
    monkeypatch.setattr(handlers, "EventLogRepository", Quiet)
    monkeypatch.setattr(handlers, "ConversationRepository", Quiet)
    return calls


def reply_request(**payload: Any) -> ActionRequest:
    message = NormalizedMessage(
        account_id=ACCOUNT,
        tg_chat_id=-100_500,
        tg_message_id=7,
        chat_type=ChatType.SUPERGROUP,
        text="нужен дизайнер",
        date=datetime.now(UTC),
        is_incoming=True,
        is_outgoing=False,
        sender_tg_id=SENDER,
    )
    return ActionRequest(
        type=ActionType.REPLY,
        account_id=ACCOUNT,
        dedup_key="REPLY:k",
        message=message,
        rule_id=uuid.uuid4(),
        reply_text="Здравствуйте!",
        payload={"lead_score": 82, "intent": "purchase", **payload},
    )


def reply_handler(sender: FakeSender) -> ReplyHandler:
    return ReplyHandler(
        RecordingDatabase(),  # type: ignore[arg-type]
        SimpleNamespace(get=lambda _account_id: object()),  # type: ignore[arg-type]
        sender,  # type: ignore[arg-type]
        NullPublisher(),  # type: ignore[arg-type]
        PeerCache(),
    )


class TestReplyHandlerNoDuplicateCard:
    def test_has_no_notifier(self) -> None:
        assert "notifier" not in {field.name for field in dataclasses.fields(ReplyHandler)}

    async def test_sends_and_saves_the_lead_without_posting_a_card(
        self, lead_calls: list[dict[str, Any]]
    ) -> None:
        sender = FakeSender()

        result = await reply_handler(sender).execute(reply_request(), uuid.uuid4())

        assert result.status is ActionStatus.SENT
        assert result.dm_error is None
        assert sender.sent == [(-100_500, "Здравствуйте!")]
        # Лид заводится как раньше: балл из payload, тот же собеседник.
        (lead,) = lead_calls
        assert lead["tg_user_id"] == SENDER
        assert lead["score"] == 82
        assert lead["intent"] == "purchase"

    async def test_failed_dm_is_returned_not_swallowed(
        self, lead_calls: list[dict[str, Any]]
    ) -> None:
        sender = FakeSender(fail_dm=True)

        result = await reply_handler(sender).execute(
            reply_request(dm_text="Развёрнутый ответ"), uuid.uuid4()
        )

        assert result.status is ActionStatus.SENT, "ответ в группу ушёл"
        assert result.dm_error == "PrivacyRestricted"
        assert len(lead_calls) == 1


async def test_action_engine_passes_the_dm_error_through() -> None:
    class Handler:
        async def execute(self, _request: ActionRequest, _action_id: uuid.UUID) -> ActionResult:
            return ActionResult(status=ActionStatus.SENT, dm_error="PrivacyRestricted")

    engine = ActionEngine(None)  # type: ignore[arg-type]
    engine.register(ActionType.REPLY, Handler())

    async def persist(_request: ActionRequest) -> tuple[uuid.UUID, bool]:
        return uuid.uuid4(), False

    async def finish(*_args: Any, **_kwargs: Any) -> None:
        return None

    engine._persist = persist  # type: ignore[method-assign]
    engine._finish = finish  # type: ignore[method-assign]

    result = await engine.dispatch(reply_request())

    assert result.dm_error == "PrivacyRestricted"
