"""Своя задержка ответа сценария: когда и как ждём перед авто-ответом.

Пауза выдерживается ДО первой отправки, в фоновой задаче и вне блокировки
аккаунта в MessageSender: handle() не должен висеть до часа, другие сообщения
и отправки — ждать чужую паузу, а cooldown/one_shot, застолбленные до паузы,
обязаны не пустить второй ответ тому же человеку, пока первый ждёт. После
паузы ответ перепроверяется: оператор мог за это время его отменить.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.actions.cooldown import CooldownClaim, CooldownGuard, CooldownKeys, CooldownVerdict
from app.actions.engine import ActionRequest, ActionResult
from app.actions.validator import ReplyValidator
from app.conversations.context import PromptContext
from app.models import (
    AccountStatus,
    ActionStatus,
    ActionType,
    ChatType,
    ConversationStatus,
    Message,
    ProcessedStatus,
    RuleScope,
    Scenario,
)
from app.pipeline.reply_pipeline import (
    SCHEDULED_REASON_MARK,
    STALE_SCHEDULED_AFTER_SECONDS,
    ReplyOutcome,
    ReplyPipeline,
    _extend_cooldown,
    reply_delay_seconds,
    stale_scheduled_query,
)
from app.rules.engine import CompiledRule, CooldownSpec, RuleMatch
from app.rules.filters import MessageFilterSpec, StopGuard
from app.rules.keywords import KeywordSpec
from app.telegram.messages import NormalizedMessage
from app.telegram.sender import MessageSender
from app.workers.worker import Worker
from tests.conftest import make_settings
from tests.fakes import FakeTelegramClient

ACCOUNT = uuid.uuid4()
OTHER_ACCOUNT = uuid.uuid4()
TG_CHAT = -100_500
SENDER = 4242
REPLY_TEXT = "Здравствуйте! Подскажем с дизайном, напишите задачу."
WAIT = 1.0
CHAT_LIMIT = 300  # chat_reply_cooldown_seconds в тестовых настройках


# --- подделки ----------------------------------------------------------------
class FakeCooldown:
    """CooldownGuard в памяти с TTL по подменённым часам (как у Redis-версии).

    TTL здесь честный: без него нельзя поймать ключ, истёкший, пока ответ
    ещё ждёт паузу.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.expires: dict[str, float] = {}

    @property
    def held(self) -> set[str]:
        return {key for key, until in self.expires.items() if until > self.now}

    def hold(self, key: str, seconds: float) -> None:
        self.expires[key] = self.now + seconds

    def ttl(self, key: str) -> float | None:
        return self.expires[key] - self.now if key in self.held else None

    async def claim(
        self, cooldown_keys: CooldownKeys, spec: CooldownSpec
    ) -> tuple[CooldownVerdict, CooldownClaim | None]:
        scopes = cooldown_keys.scopes(spec)
        held = self.held
        for scope, key, _seconds in scopes:
            if key in held:
                return CooldownVerdict(allowed=False, blocked_by=scope), None
        for _scope, key, seconds in scopes:
            self.hold(key, seconds)
        redis_keys = tuple(key for _scope, key, _seconds in scopes)
        return CooldownVerdict(allowed=True), CooldownClaim(token="t", redis_keys=redis_keys)

    async def release(self, claim: CooldownClaim | None) -> None:
        if claim is not None:
            for key in claim.redis_keys:
                self.expires.pop(key, None)

    async def claim_once(self, key: str, seconds: int) -> bool:
        if seconds <= 0:
            return True
        if key in self.held:
            return False
        self.hold(key, seconds)
        return True

    async def is_held(self, key: str) -> bool:
        return key in self.held

    async def release_once(self, key: str) -> None:
        self.expires.pop(key, None)

    async def extend_once(self, key: str, seconds: int) -> None:
        if seconds > 0:
            self.hold(key, seconds)


class FakeActions:
    """ActionEngine без базы: запоминает запросы, REPLY отдаёт в on_reply."""

    def __init__(
        self,
        on_reply: Callable[[ActionRequest], Awaitable[None]] | None = None,
        reply_status: ActionStatus = ActionStatus.SENT,
        on_escalate: Callable[[ActionRequest], Awaitable[None]] | None = None,
    ) -> None:
        self.requests: list[ActionRequest] = []
        self._on_reply = on_reply
        self._on_escalate = on_escalate
        self._reply_status = reply_status

    async def dispatch(self, request: ActionRequest) -> ActionResult:
        self.requests.append(request)
        status = ActionStatus.SENT
        if request.type is ActionType.REPLY:
            if self._on_reply is not None:
                await self._on_reply(request)
            status = self._reply_status
        if request.type is ActionType.ESCALATE_TO_HUMAN and self._on_escalate is not None:
            await self._on_escalate(request)
        return ActionResult(status=status, action_id=uuid.uuid4(), detail="fake")

    def of(self, action: ActionType) -> list[ActionRequest]:
        return [request for request in self.requests if request.type is action]


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)


class FakeDatabase:
    """Сессия в памяти: строки по id (сообщения, правила, сценарии, чаты,
    диалоги); MessageRepository.set_status меняет строку сообщения."""

    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, Any] = {}
        self.fail_get = False
        self.stale: list[Any] = []
        self.statements: list[Any] = []

    def add_message(self) -> uuid.UUID:
        message_id = uuid.uuid4()
        self.rows[message_id] = SimpleNamespace(
            processed_status=ProcessedStatus.MATCHED, rule_id=None, status_reason=None
        )
        return message_id

    def add(self, row_id: uuid.UUID, **fields: Any) -> SimpleNamespace:
        row = SimpleNamespace(**fields)
        self.rows[row_id] = row
        return row

    @contextlib.asynccontextmanager
    async def session(self) -> AsyncIterator[Any]:
        yield self

    async def get(self, model: object, ident: uuid.UUID) -> Any:
        # fail_get роняет чтение правил/сценариев/чатов/диалогов (перепроверку),
        # но не запись статуса сообщения — её тест проверяет.
        if self.fail_get and model is not Message:
            raise RuntimeError("db is down")
        return self.rows.get(ident)

    async def scalars(self, statement: Any) -> _Rows:
        self.statements.append(statement)
        return _Rows(self.stale)

    async def scalar(self, _statement: Any) -> Any:
        return None

    async def flush(self) -> None:
        return None


class GatedSleep:
    """Подмена asyncio.sleep: запоминает паузу и ждёт, пока тест не отпустит."""

    def __init__(self, on_call: Callable[[], None] | None = None) -> None:
        self.calls: list[float] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self._on_call = on_call

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self._on_call is not None:
            self._on_call()
        self.started.set()
        await self.release.wait()


# --- сборка ------------------------------------------------------------------
@dataclass
class Env:
    pipeline: ReplyPipeline
    cooldown: FakeCooldown
    actions: FakeActions
    database: FakeDatabase
    sleep: GatedSleep
    stop_guard: StopGuard


def make_env(sleep: GatedSleep | None = None, actions: FakeActions | None = None) -> Env:
    cooldown = FakeCooldown()
    database = FakeDatabase()
    sleep = sleep or GatedSleep()
    actions = actions or FakeActions()
    stop_guard = StopGuard()
    pipeline = ReplyPipeline(
        make_settings(anti_duplicate_ttl_seconds=0, chat_reply_cooldown_seconds=CHAT_LIMIT),
        database,  # type: ignore[arg-type]
        analyzer=None,
        generator=None,
        context=None,  # type: ignore[arg-type]
        validator=ReplyValidator(),
        cooldown=cooldown,  # type: ignore[arg-type]
        actions=actions,  # type: ignore[arg-type]
        sleep=sleep,
        stop_guard=stop_guard,
    )
    return Env(pipeline, cooldown, actions, database, sleep, stop_guard)


def make_scenario(**fields: Any) -> Scenario:
    defaults: dict[str, Any] = {
        "name": "Продажи",
        "system_prompt": "Ты менеджер",
        "require_knowledge_grounding": False,
        "reply_in_dm": False,
        "one_shot": False,
        "fallback_texts": [],
        "max_reply_length": 500,
    }
    return Scenario(id=uuid.uuid4(), **{**defaults, **fields})


def make_match(cooldown: CooldownSpec | None = None) -> RuleMatch:
    return RuleMatch(
        rule=CompiledRule(
            id=uuid.uuid4(),
            name="Поиск клиентов",
            priority=1,
            stop_on_match=True,
            scope=RuleScope.CHAT_MONITOR,
            scenario_id=None,
            action=ActionType.REPLY,
            action_config={},
            filters=MessageFilterSpec(),
            keywords=KeywordSpec(),
            regex=None,
            ai_enabled=False,
            ai_threshold=None,
            cooldown=cooldown if cooldown is not None else CooldownSpec(user=600),
        )
    )


def make_message(
    tg_message_id: int = 1,
    sender: int = SENDER,
    chat_type: ChatType = ChatType.SUPERGROUP,
    account_id: uuid.UUID = ACCOUNT,
) -> NormalizedMessage:
    return NormalizedMessage(
        account_id=account_id,
        tg_chat_id=TG_CHAT,
        tg_message_id=tg_message_id,
        chat_type=chat_type,
        text="нужен дизайнер",
        date=datetime.now(UTC),
        is_incoming=True,
        is_outgoing=False,
        sender_tg_id=sender,
    )


def chat_limit_key(account_id: uuid.UUID = ACCOUNT) -> str:
    return f"chatcd:{account_id}:{TG_CHAT}"


@dataclass
class Sent:
    outcome: ReplyOutcome
    message_id: uuid.UUID
    match: RuleMatch


async def send(
    env: Env,
    scenario: Scenario,
    *,
    message: NormalizedMessage | None = None,
    match: RuleMatch | None = None,
    one_shot_peer: int | None = None,
    review: bool = False,
    chat_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
    chat_limit: bool = False,
    card: Any = None,
) -> Sent:
    """Ответ, прошедший анализ и генерацию: вход в стадию отправки.

    Правило и сценарий заводятся в «базе» включёнными — как при получении
    сообщения; перепроверка после паузы читает их оттуда.
    """
    message = message or make_message()
    match = match or make_match()
    env.database.rows.setdefault(match.rule.id, SimpleNamespace(enabled=True))
    env.database.rows.setdefault(scenario.id, scenario)
    key: str | None = None
    if chat_limit:
        # Лимит на чат столбится при получении сообщения, в handle().
        key = chat_limit_key(message.account_id)
        assert await env.cooldown.claim_once(key, CHAT_LIMIT)
    message_id = env.database.add_message()
    outcome = await env.pipeline._send(
        message,
        match,
        chat_id,
        message_id,
        PromptContext(conversation_id=conversation_id),
        scenario,
        text=REPLY_TEXT,
        used_knowledge=False,
        cooldown_keys=CooldownKeys(
            account_id=message.account_id,
            tg_chat_id=message.tg_chat_id,
            peer_tg_id=message.sender_tg_id,
            rule_id=match.rule.id,
            scenario_id=scenario.id,
        ),
        analysis=None,
        review=review,
        one_shot_peer=one_shot_peer,
        chat_limit_key=key,
        card=card,
    )
    return Sent(outcome, message_id, match)


def delayed(seconds: int = 30, **fields: Any) -> Scenario:
    return make_scenario(reply_delay_min_seconds=seconds, reply_delay_max_seconds=seconds, **fields)


async def drain(env: Env) -> None:
    await asyncio.wait_for(asyncio.gather(*list(env.pipeline._delayed)), timeout=WAIT)


async def settle() -> None:
    """Даёт отработать done-колбэкам задач."""
    for _ in range(3):
        await asyncio.sleep(0)


# --- расчёт паузы --------------------------------------------------------------
class TestReplyDelaySeconds:
    @pytest.mark.parametrize(("low", "high"), [(None, None), (0, 0), (0, None), (None, 0)])
    def test_no_delay_when_unset_or_zero(self, low: int | None, high: int | None) -> None:
        scenario = make_scenario(reply_delay_min_seconds=low, reply_delay_max_seconds=high)
        assert reply_delay_seconds(scenario) == 0.0

    def test_random_value_within_range(self) -> None:
        scenario = make_scenario(reply_delay_min_seconds=10, reply_delay_max_seconds=20)
        values = {reply_delay_seconds(scenario) for _ in range(200)}
        assert all(10 <= value <= 20 for value in values)
        assert len(values) > 1, "пауза должна быть случайной, а не фиксированной"

    def test_only_min_means_exactly_min(self) -> None:
        """Правило «заполнено только «от» — ровно «от»»: панель сохраняет его
        явно (до = от), но и строка с пустым «до» ведёт себя так же."""
        scenario = make_scenario(reply_delay_min_seconds=15, reply_delay_max_seconds=None)
        assert reply_delay_seconds(scenario) == 15.0

    def test_only_max_means_from_zero(self) -> None:
        scenario = make_scenario(reply_delay_min_seconds=None, reply_delay_max_seconds=30)
        assert all(0 <= reply_delay_seconds(scenario) <= 30 for _ in range(50))


class TestExtendCooldown:
    def test_every_set_scope_grows_by_the_delay_and_margin(self) -> None:
        spec = CooldownSpec(user=600, chat=0, account=30, rule=0, scenario=120)
        extended = _extend_cooldown(spec, 99.2)
        extra = 100 + 60  # ceil(паузы) + запас на саму отправку
        assert extended == CooldownSpec(
            user=600 + extra, chat=0, account=30 + extra, rule=0, scenario=120 + extra
        )

    def test_no_delay_keeps_the_spec(self) -> None:
        spec = CooldownSpec(user=600)
        assert _extend_cooldown(spec, 0.0) is spec


# --- отложенная отправка -------------------------------------------------------
class TestDelayedReply:
    async def test_waits_in_background_before_the_first_send(self) -> None:
        env = make_env()
        scenario = make_scenario(reply_delay_min_seconds=10, reply_delay_max_seconds=20)

        sent = await send(env, scenario)

        # handle() не ждёт паузу: вернулся сразу, ответ запланирован.
        assert sent.outcome.scheduled
        assert sent.outcome.processed_status is ProcessedStatus.MATCHED
        await asyncio.wait_for(env.sleep.started.wait(), timeout=WAIT)
        assert len(env.sleep.calls) == 1
        assert 10 <= env.sleep.calls[0] <= 20
        assert env.actions.of(ActionType.REPLY) == [], "до конца паузы ничего не уходит"
        row = env.database.rows[sent.message_id]
        assert row.processed_status is ProcessedStatus.MATCHED
        assert SCHEDULED_REASON_MARK in row.status_reason

        env.sleep.release.set()
        await drain(env)

        assert len(env.actions.of(ActionType.REPLY)) == 1
        assert row.processed_status is ProcessedStatus.REPLIED
        await settle()
        assert not env.pipeline._delayed

    @pytest.mark.parametrize(("low", "high"), [(None, None), (0, 0), (0, None), (None, 0)])
    async def test_no_wait_when_delay_is_unset(self, low: int | None, high: int | None) -> None:
        env = make_env()
        scenario = make_scenario(reply_delay_min_seconds=low, reply_delay_max_seconds=high)

        sent = await send(env, scenario, chat_limit=True)

        assert env.sleep.calls == []
        assert sent.outcome.action is ActionType.REPLY
        assert sent.outcome.status is ActionStatus.SENT
        assert len(env.actions.of(ActionType.REPLY)) == 1
        assert not env.pipeline._delayed
        # Без паузы ни кулдаун, ни лимит чата не удлиняются.
        assert env.cooldown.ttl(chat_limit_key()) == CHAT_LIMIT

    async def test_wait_is_not_under_the_sender_lock(self) -> None:
        """Пока ответ ждёт паузу, аккаунт свободен: другие отправки идут сразу."""
        sender = MessageSender(
            make_settings(
                send_min_interval_seconds=0,
                reply_typing_delay_min_seconds=0,
                reply_typing_delay_max_seconds=0,
            )
        )
        client = FakeTelegramClient()
        lock_held_during_sleep: list[bool] = []

        def on_sleep() -> None:
            lock = sender._locks.get(ACCOUNT)
            lock_held_during_sleep.append(lock is not None and lock.locked())

        async def on_reply(request: ActionRequest) -> None:
            assert request.message is not None
            await sender.send(
                request.account_id,
                client,
                chat_id=request.message.tg_chat_id,
                text=request.reply_text or "",
                reply_to=request.message.tg_message_id,
            )

        env = make_env(sleep=GatedSleep(on_call=on_sleep), actions=FakeActions(on_reply))
        await send(env, make_scenario(reply_delay_min_seconds=5, reply_delay_max_seconds=5))
        await asyncio.wait_for(env.sleep.started.wait(), timeout=WAIT)

        # Ручная отправка оператора с того же аккаунта не ждёт чужую паузу.
        await asyncio.wait_for(
            sender.send(ACCOUNT, client, chat_id=TG_CHAT, text="оператор"), timeout=WAIT
        )
        assert [text for _chat, text, _reply in client.sent] == ["оператор"]

        env.sleep.release.set()
        await drain(env)

        assert lock_held_during_sleep == [False]
        assert [text for _chat, text, _reply in client.sent] == ["оператор", REPLY_TEXT]

    async def test_other_messages_are_processed_during_the_wait(self) -> None:
        env = make_env()
        await send(env, make_scenario(reply_delay_min_seconds=30, reply_delay_max_seconds=60))
        await asyncio.wait_for(env.sleep.started.wait(), timeout=WAIT)

        other = await send(env, make_scenario(), message=make_message(2, sender=SENDER + 1))

        assert other.outcome.status is ActionStatus.SENT
        assert len(env.actions.of(ActionType.REPLY)) == 1, "второй ответ ушёл, первый ждёт"

        env.sleep.release.set()
        await drain(env)
        assert len(env.actions.of(ActionType.REPLY)) == 2

    async def test_cooldown_claimed_before_the_wait_blocks_a_second_reply(self) -> None:
        env = make_env()
        scenario = delayed(30)
        match = make_match(CooldownSpec(user=600))

        await send(env, scenario, match=match)
        await asyncio.wait_for(env.sleep.started.wait(), timeout=WAIT)
        second = await send(env, scenario, match=match, message=make_message(2))

        assert second.outcome.action is ActionType.IGNORE
        assert "cooldown" in (second.outcome.reason or "")

        env.sleep.release.set()
        await drain(env)
        assert len(env.actions.of(ActionType.REPLY)) == 1

    async def test_cooldown_outlives_a_delay_longer_than_the_rule_cooldown(self) -> None:
        """Пауза 1500 с при кулдауне 600 с: через 700 с второй ответ не пускаем."""
        env = make_env()
        scenario = delayed(1500)
        match = make_match(CooldownSpec(user=600))

        await send(env, scenario, match=match)
        await asyncio.wait_for(env.sleep.started.wait(), timeout=WAIT)
        user_key = next(iter(env.cooldown.held))
        # Отсчёт кулдауна — от реальной отправки: пауза + кулдаун + запас.
        assert env.cooldown.ttl(user_key) == 1500 + 600 + 60

        env.cooldown.now = 700  # кулдаун правила без удлинения уже истёк бы
        second = await send(env, scenario, match=match, message=make_message(2))

        assert second.outcome.action is ActionType.IGNORE
        assert second.outcome.reason == "cooldown: user"
        env.sleep.release.set()
        await drain(env)
        assert len(env.actions.of(ActionType.REPLY)) == 1

    async def test_chat_limit_is_held_until_the_delayed_send(self) -> None:
        """Анти-бан лимит чата отсчитывается от отправки, а не от получения."""
        env = make_env()
        await send(env, delayed(1000), chat_limit=True)
        await asyncio.wait_for(env.sleep.started.wait(), timeout=WAIT)

        assert env.cooldown.ttl(chat_limit_key()) == CHAT_LIMIT + 1000 + 60
        env.cooldown.now = 400  # без продления лимит уже истёк бы
        # Сообщение другого человека в ту же группу упирается в лимит.
        assert not await env.cooldown.claim_once(chat_limit_key(), CHAT_LIMIT)

        env.sleep.release.set()
        await drain(env)
        assert len(env.actions.of(ActionType.REPLY)) == 1
        assert chat_limit_key() in env.cooldown.held, "после отправки лимит держится"

    async def test_one_shot_mark_blocks_a_second_reply_during_the_wait(self) -> None:
        """Лид заводится только на отправке — до неё one_shot держит метка."""
        env = make_env()
        scenario = delayed(30, one_shot=True)
        match = make_match(CooldownSpec())  # без кулдауна: проверяем именно one_shot

        await send(env, scenario, match=match, one_shot_peer=SENDER)
        await asyncio.wait_for(env.sleep.started.wait(), timeout=WAIT)
        second = await send(
            env, scenario, match=match, message=make_message(2), one_shot_peer=SENDER
        )

        assert second.outcome.action is ActionType.IGNORE
        assert second.outcome.reason == "one_shot: ответ уже запланирован"

        env.sleep.release.set()
        await drain(env)
        assert len(env.actions.of(ActionType.REPLY)) == 1
        assert env.cooldown.held == set(), "метка снимается после отправки"

    async def test_reply_in_dm_waits_once_before_the_group_ack(self) -> None:
        env = make_env()
        scenario = delayed(3, reply_in_dm=True, group_ack_text="Отправлю в лс")

        await send(env, scenario)
        env.sleep.release.set()
        await drain(env)

        assert env.sleep.calls == [3.0]
        replies = env.actions.of(ActionType.REPLY)
        # Одно действие: сначала фраза в группу, личка — сразу следом (ReplyHandler).
        assert len(replies) == 1
        assert replies[0].reply_text == "Отправлю в лс"
        assert replies[0].payload["dm_text"] == REPLY_TEXT

    async def test_review_is_not_delayed(self) -> None:
        env = make_env()

        sent = await send(env, delayed(30), review=True)

        assert sent.outcome.action is ActionType.REQUEST_REVIEW
        assert env.sleep.calls == []
        assert not env.pipeline._delayed

    async def test_failed_send_after_the_wait_releases_cooldown_and_escalates(self) -> None:
        env = make_env(actions=FakeActions(reply_status=ActionStatus.FAILED))
        sent = await send(env, delayed(1))

        env.sleep.release.set()
        await drain(env)

        assert env.cooldown.held == set()
        assert len(env.actions.of(ActionType.ESCALATE_TO_HUMAN)) == 1
        assert env.database.rows[sent.message_id].processed_status is ProcessedStatus.ESCALATED

    async def test_crash_during_delayed_send_is_caught_and_escalated(self) -> None:
        async def boom(_request: ActionRequest) -> None:
            raise RuntimeError("db is down")

        env = make_env(actions=FakeActions(on_reply=boom))
        sent = await send(env, delayed(1))

        env.sleep.release.set()
        await drain(env)  # задача не падает наружу

        escalations = env.actions.of(ActionType.ESCALATE_TO_HUMAN)
        assert len(escalations) == 1
        assert "db is down" in escalations[0].payload["reason"]
        # Отправка уже начиналась — ответ мог уйти, кулдаун не снимаем.
        assert env.cooldown.held
        assert env.database.rows[sent.message_id].processed_status is ProcessedStatus.ESCALATED


# --- перепроверка после паузы --------------------------------------------------
CHAT_ID = uuid.uuid4()
CONVERSATION_ID = uuid.uuid4()


def _stoplist(env: Env, sent: Sent, _scenario: Scenario) -> None:
    env.stop_guard.update([SENDER], [])


def _rule_disabled(env: Env, sent: Sent, _scenario: Scenario) -> None:
    env.database.rows[sent.match.rule.id].enabled = False


def _rule_deleted(env: Env, sent: Sent, _scenario: Scenario) -> None:
    del env.database.rows[sent.match.rule.id]


def _scenario_disabled(env: Env, _sent: Sent, scenario: Scenario) -> None:
    scenario.enabled = False


def _scenario_deleted(env: Env, _sent: Sent, scenario: Scenario) -> None:
    del env.database.rows[scenario.id]


def _chat_unmonitored(env: Env, _sent: Sent, _scenario: Scenario) -> None:
    env.database.rows[CHAT_ID].monitored = False


def _handed_over(env: Env, _sent: Sent, _scenario: Scenario) -> None:
    env.database.rows[CONVERSATION_ID].status = ConversationStatus.HUMAN_REQUIRED


def _closed(env: Env, _sent: Sent, _scenario: Scenario) -> None:
    env.database.rows[CONVERSATION_ID].status = ConversationStatus.CLOSED


class TestRevalidationAfterTheWait:
    def _env(self) -> Env:
        env = make_env()
        env.database.add(CHAT_ID, monitored=True)
        env.database.add(CONVERSATION_ID, status=ConversationStatus.ACTIVE)
        return env

    @pytest.mark.parametrize(
        ("change", "reason"),
        [
            (_stoplist, "стоп-лист"),
            (_rule_disabled, "правило выключено"),
            (_rule_deleted, "правило выключено"),
            (_scenario_disabled, "сценарий выключен"),
            (_scenario_deleted, "сценарий удалён"),
            (_chat_unmonitored, "мониторинг чата выключен"),
            (_handed_over, "диалог передан оператору"),
            (_closed, "диалог передан оператору"),
        ],
    )
    async def test_operator_change_during_the_wait_cancels_the_reply(
        self, change: Callable[[Env, Sent, Scenario], None], reason: str
    ) -> None:
        env = self._env()
        scenario = delayed(600, one_shot=True)
        sent = await send(
            env,
            scenario,
            chat_id=CHAT_ID,
            conversation_id=CONVERSATION_ID,
            one_shot_peer=SENDER,
            chat_limit=True,
        )
        await asyncio.wait_for(env.sleep.started.wait(), timeout=WAIT)

        change(env, sent, scenario)
        env.sleep.release.set()
        await drain(env)

        assert env.actions.of(ActionType.REPLY) == [], "отменённый ответ не уходит"
        ignores = env.actions.of(ActionType.IGNORE)
        assert len(ignores) == 1
        assert reason in ignores[0].payload["reason"]
        row = env.database.rows[sent.message_id]
        assert row.processed_status is ProcessedStatus.IGNORED
        assert reason in row.status_reason
        # Ответа не было: ни кулдаун, ни лимит чата, ни метка one_shot не висят.
        assert env.cooldown.held == set()

    async def test_conversation_already_with_a_human_does_not_block(self) -> None:
        """Без паузы ответ ушёл бы и такому диалогу — с паузой не строже."""
        env = self._env()
        env.database.rows[CONVERSATION_ID].status = ConversationStatus.HUMAN_REQUIRED

        await send(env, delayed(10), chat_id=CHAT_ID, conversation_id=CONVERSATION_ID)
        env.sleep.release.set()
        await drain(env)

        assert len(env.actions.of(ActionType.REPLY)) == 1

    async def test_disabled_scenario_is_not_re_checked_if_it_was_disabled_already(
        self,
    ) -> None:
        env = self._env()
        await send(env, delayed(10, enabled=False))
        env.sleep.release.set()
        await drain(env)

        assert len(env.actions.of(ActionType.REPLY)) == 1

    async def test_private_chat_does_not_need_monitoring(self) -> None:
        env = self._env()
        env.database.rows[CHAT_ID].monitored = False

        await send(
            env,
            delayed(10),
            chat_id=CHAT_ID,
            message=make_message(chat_type=ChatType.PRIVATE),
        )
        env.sleep.release.set()
        await drain(env)

        assert len(env.actions.of(ActionType.REPLY)) == 1

    async def test_failed_re_check_escalates_instead_of_sending_blind(self) -> None:
        env = self._env()
        sent = await send(env, delayed(10), chat_id=CHAT_ID)
        await asyncio.wait_for(env.sleep.started.wait(), timeout=WAIT)

        env.database.fail_get = True
        env.sleep.release.set()
        await drain(env)
        env.database.fail_get = False

        assert env.actions.of(ActionType.REPLY) == []
        escalations = env.actions.of(ActionType.ESCALATE_TO_HUMAN)
        assert len(escalations) == 1
        assert "не перепроверен" in escalations[0].payload["reason"]
        assert env.cooldown.held == set()
        assert env.database.rows[sent.message_id].processed_status is ProcessedStatus.ESCALATED


# --- остановка и отключение аккаунта -------------------------------------------
class TestShutdown:
    async def test_cancels_the_wait_releases_cooldown_and_escalates(self) -> None:
        env = make_env()
        sent = await send(env, delayed(600, one_shot=True), one_shot_peer=SENDER, chat_limit=True)
        await asyncio.wait_for(env.sleep.started.wait(), timeout=WAIT)

        await env.pipeline.shutdown(grace_seconds=WAIT)

        assert env.actions.of(ActionType.REPLY) == []
        escalations = env.actions.of(ActionType.ESCALATE_TO_HUMAN)
        assert len(escalations) == 1
        assert "остановка воркера" in escalations[0].payload["reason"]
        assert env.cooldown.held == set(), "ни кулдаун, ни лимит чата, ни метка one_shot"
        assert env.database.rows[sent.message_id].processed_status is ProcessedStatus.ESCALATED
        assert not env.pipeline._delayed

    async def test_task_that_has_not_started_yet_still_cleans_up(self) -> None:
        """Остановка сразу после планирования: задача ещё не сделала ни шага.

        cancel() такой задачи бросил бы CancelledError до входа в корутину —
        без эскалации и со всем застолбленным.
        """
        env = make_env()
        sent = await send(env, delayed(600, one_shot=True), one_shot_peer=SENDER)
        assert env.sleep.calls == [], "задача ещё не стартовала"

        await env.pipeline.shutdown(grace_seconds=WAIT)

        assert env.sleep.calls == []
        escalations = env.actions.of(ActionType.ESCALATE_TO_HUMAN)
        assert len(escalations) == 1
        assert "остановка воркера" in escalations[0].payload["reason"]
        assert env.cooldown.held == set()
        assert env.database.rows[sent.message_id].processed_status is ProcessedStatus.ESCALATED
        assert not env.pipeline._delayed

    async def test_no_new_delayed_replies_after_shutdown(self) -> None:
        env = make_env()
        await env.pipeline.shutdown()

        sent = await send(env, delayed(5), chat_limit=True)

        assert sent.outcome.action is ActionType.ESCALATE_TO_HUMAN
        assert env.sleep.calls == []
        assert env.actions.of(ActionType.REPLY) == []
        assert env.cooldown.held == set()
        assert not env.pipeline._delayed

    async def test_shutdown_starting_while_the_status_is_written_is_not_missed(self) -> None:
        """Гонка: shutdown() начался, пока _schedule писал промежуточный статус.

        Задача, созданная после его снимка, проспала бы до закрытия базы —
        без эскалации, с висящими кулдауном и меткой.
        """
        env = make_env()
        pipeline = env.pipeline
        original = pipeline._mark_scheduled

        async def mark_and_shutdown(*args: Any) -> Any:
            result = await original(*args)
            await pipeline.shutdown(grace_seconds=WAIT)  # набор ещё пуст
            return result

        pipeline._mark_scheduled = mark_and_shutdown  # type: ignore[method-assign]
        sent = await send(env, delayed(600, one_shot=True), one_shot_peer=SENDER)

        assert sent.outcome.action is ActionType.ESCALATE_TO_HUMAN
        assert not pipeline._delayed
        assert env.sleep.calls == []
        assert env.cooldown.held == set()

    async def test_waits_for_tasks_already_cancelled_for_an_account(self) -> None:
        """Уборка задачи, отменённой по отключению аккаунта, не прерывается
        повторной отменой при остановке воркера."""
        cleanup_started = asyncio.Event()
        cleanup_release = asyncio.Event()

        async def slow_escalation(_request: ActionRequest) -> None:
            cleanup_started.set()
            await cleanup_release.wait()

        env = make_env(actions=FakeActions(on_escalate=slow_escalation))
        sent = await send(env, delayed(600))
        await asyncio.wait_for(env.sleep.started.wait(), timeout=WAIT)

        cancel = asyncio.create_task(env.pipeline.cancel_account(ACCOUNT, "аккаунт отключён"))
        await asyncio.wait_for(cleanup_started.wait(), timeout=WAIT)
        shutdown = asyncio.create_task(env.pipeline.shutdown(grace_seconds=WAIT))
        await settle()
        cleanup_release.set()
        await asyncio.wait_for(asyncio.gather(cancel, shutdown), timeout=WAIT)

        escalations = env.actions.of(ActionType.ESCALATE_TO_HUMAN)
        assert len(escalations) == 1
        assert "аккаунт отключён" in escalations[0].payload["reason"]
        assert env.database.rows[sent.message_id].processed_status is ProcessedStatus.ESCALATED


class TestCancelAccount:
    async def test_cancels_only_the_waiting_replies_of_that_account(self) -> None:
        env = make_env()
        mine = await send(env, delayed(600), chat_limit=True)
        other_message = make_message(2, sender=SENDER + 1, account_id=OTHER_ACCOUNT)
        await send(env, delayed(600), message=other_message)
        await asyncio.wait_for(env.sleep.started.wait(), timeout=WAIT)

        cancelled = await env.pipeline.cancel_account(ACCOUNT, "аккаунт отключён от воркера")
        await settle()

        assert cancelled == 1
        escalations = env.actions.of(ActionType.ESCALATE_TO_HUMAN)
        assert len(escalations) == 1
        assert escalations[0].account_id == ACCOUNT
        assert "аккаунт отключён от воркера" in escalations[0].payload["reason"]
        assert env.database.rows[mine.message_id].processed_status is ProcessedStatus.ESCALATED
        assert chat_limit_key() not in env.cooldown.held
        assert len(env.pipeline._delayed) == 1, "ответ другого аккаунта ждёт дальше"

        env.sleep.release.set()
        await drain(env)
        replies = env.actions.of(ActionType.REPLY)
        assert [request.account_id for request in replies] == [OTHER_ACCOUNT]

    async def test_reply_already_being_sent_is_not_interrupted(self) -> None:
        sending = asyncio.Event()
        finish = asyncio.Event()

        async def slow_send(_request: ActionRequest) -> None:
            sending.set()
            await finish.wait()

        env = make_env(actions=FakeActions(on_reply=slow_send))
        await send(env, delayed(10))
        env.sleep.release.set()
        await asyncio.wait_for(sending.wait(), timeout=WAIT)

        assert await env.pipeline.cancel_account(ACCOUNT, "аккаунт отключён") == 0

        finish.set()
        await drain(env)
        assert env.actions.of(ActionType.ESCALATE_TO_HUMAN) == []

    @pytest.mark.parametrize(
        ("status", "client_gone", "reason"),
        [
            (AccountStatus.OFFLINE, True, "аккаунт отключён от воркера"),
            (AccountStatus.AUTH_REQUIRED, False, "аккаунту нужна повторная авторизация"),
            (AccountStatus.OFFLINE, False, None),  # обрыв связи: переподключится сам
            (AccountStatus.ONLINE, False, None),
        ],
    )
    async def test_worker_cancels_when_the_account_leaves(
        self, status: AccountStatus, client_gone: bool, reason: str | None
    ) -> None:
        calls: list[tuple[uuid.UUID, str]] = []

        class Pipeline:
            async def cancel_account(self, account_id: uuid.UUID, why: str) -> int:
                calls.append((account_id, why))
                return 0

        class Accounts:
            async def on_client_status(self, *_args: Any) -> None:
                return None

        worker = SimpleNamespace(
            _accounts=Accounts(),
            _reply_pipeline=Pipeline(),
            _clients=SimpleNamespace(health=lambda _account_id: None if client_gone else object()),
        )

        await Worker._on_account_status(worker, ACCOUNT, status, None)  # type: ignore[arg-type]

        assert calls == ([] if reason is None else [(ACCOUNT, reason)])


# --- зависшие после жёсткого падения -------------------------------------------
class TestStaleScheduledSweep:
    def _row(self, **fields: Any) -> SimpleNamespace:
        defaults: dict[str, Any] = {
            "id": uuid.uuid4(),
            "account_id": ACCOUNT,
            "chat_id": uuid.uuid4(),
            "conversation_id": uuid.uuid4(),
            "tg_chat_id": TG_CHAT,
            "tg_message_id": 77,
            "sender_tg_id": SENDER,
            "rule_id": uuid.uuid4(),
            "processed_status": ProcessedStatus.MATCHED,
            "status_reason": f"ответ через 900 с {SCHEDULED_REASON_MARK}",
        }
        return SimpleNamespace(**{**defaults, **fields})

    async def test_escalates_stuck_messages_once_per_message(self) -> None:
        env = make_env()
        row = self._row()
        env.database.stale = [row]
        env.database.rows[row.id] = row

        assert await env.pipeline.sweep_stale_scheduled() == 1

        escalations = env.actions.of(ActionType.ESCALATE_TO_HUMAN)
        assert len(escalations) == 1
        request = escalations[0]
        # Тот же ключ, что у обычной эскалации этого сообщения: второй воркер
        # или повторный проход не заведут вторую.
        assert request.dedup_key == f"{ActionType.ESCALATE_TO_HUMAN.value}:{ACCOUNT}:{TG_CHAT}:77"
        assert request.message_id == row.id
        assert request.conversation_id == row.conversation_id
        assert "воркер упал" in request.payload["reason"]
        assert row.processed_status is ProcessedStatus.ESCALATED

    async def test_skips_replies_still_waiting_on_this_worker(self) -> None:
        env = make_env()
        sent = await send(env, delayed(600))
        await asyncio.wait_for(env.sleep.started.wait(), timeout=WAIT)
        env.database.stale = [self._row(id=sent.message_id)]

        assert await env.pipeline.sweep_stale_scheduled() == 0
        assert env.actions.of(ActionType.ESCALATE_TO_HUMAN) == []

        env.sleep.release.set()
        await drain(env)

    def test_query_targets_only_unfinished_scheduled_replies(self) -> None:
        now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
        sql = str(
            stale_scheduled_query(now, limit=100).compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )
        assert "messages.processed_status = 'MATCHED'" in sql
        assert "задержка сценария" in sql
        assert "NOT (EXISTS" in sql
        assert "actions.message_id = messages.id" in sql
        assert "'ESCALATE_TO_HUMAN'" in sql and "'IGNORE'" in sql
        assert "actions.status = 'SENT'" in sql
        assert "LIMIT 100" in sql
        # Живая задача ждёт не дольше потолка паузы — запас сверх него.
        assert STALE_SCHEDULED_AFTER_SECONDS > 3600


# --- CooldownGuard -------------------------------------------------------------
class TestCooldownGuardExtendOnce:
    async def test_sets_the_key_for_the_new_ttl(self) -> None:
        calls: list[tuple[str, str, int | None]] = []

        class Redis:
            def register_script(self, _script: str) -> object:
                return object()

            async def set(self, key: str, value: str, ex: int | None = None) -> bool:
                calls.append((key, value, ex))
                return True

        guard = CooldownGuard(Redis())  # type: ignore[arg-type]
        await guard.extend_once("chatcd:a:1", 1360)
        await guard.extend_once("chatcd:a:1", 0)

        assert calls == [("chatcd:a:1", "1", 1360)]
