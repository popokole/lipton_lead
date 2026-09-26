"""Своя задержка ответа сценария: когда и как ждём перед авто-ответом.

Пауза выдерживается ДО первой отправки, в фоновой задаче и вне блокировки
аккаунта в MessageSender: handle() не должен висеть до часа, другие сообщения
и отправки — ждать чужую паузу, а cooldown/one_shot, застолбленные до паузы,
обязаны не пустить второй ответ тому же человеку, пока первый ждёт.
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

from app.actions.cooldown import CooldownClaim, CooldownKeys, CooldownVerdict
from app.actions.engine import ActionRequest, ActionResult
from app.actions.validator import ReplyValidator
from app.conversations.context import PromptContext
from app.models import ActionStatus, ActionType, ChatType, ProcessedStatus, RuleScope, Scenario
from app.pipeline.reply_pipeline import ReplyOutcome, ReplyPipeline, reply_delay_seconds
from app.rules.engine import CompiledRule, CooldownSpec, RuleMatch
from app.rules.filters import MessageFilterSpec
from app.rules.keywords import KeywordSpec
from app.telegram.messages import NormalizedMessage
from app.telegram.sender import MessageSender
from tests.conftest import make_settings
from tests.fakes import FakeTelegramClient

ACCOUNT = uuid.uuid4()
TG_CHAT = -100_500
SENDER = 4242
REPLY_TEXT = "Здравствуйте! Подскажем с дизайном, напишите задачу."
WAIT = 1.0


# --- подделки ----------------------------------------------------------------
class FakeCooldown:
    """CooldownGuard в памяти: те же ответы, что у Redis-версии."""

    def __init__(self) -> None:
        self.held: set[str] = set()

    async def claim(
        self, cooldown_keys: CooldownKeys, spec: CooldownSpec
    ) -> tuple[CooldownVerdict, CooldownClaim | None]:
        scopes = cooldown_keys.scopes(spec)
        for scope, key, _seconds in scopes:
            if key in self.held:
                return CooldownVerdict(allowed=False, blocked_by=scope), None
        redis_keys = tuple(key for _scope, key, _seconds in scopes)
        self.held.update(redis_keys)
        return CooldownVerdict(allowed=True), CooldownClaim(token="t", redis_keys=redis_keys)

    async def release(self, claim: CooldownClaim | None) -> None:
        if claim is not None:
            self.held.difference_update(claim.redis_keys)

    async def claim_once(self, key: str, seconds: int) -> bool:
        if seconds <= 0:
            return True
        if key in self.held:
            return False
        self.held.add(key)
        return True

    async def is_held(self, key: str) -> bool:
        return key in self.held

    async def release_once(self, key: str) -> None:
        self.held.discard(key)


class FakeActions:
    """ActionEngine без базы: запоминает запросы, REPLY отдаёт в on_reply."""

    def __init__(
        self,
        on_reply: Callable[[ActionRequest], Awaitable[None]] | None = None,
        reply_status: ActionStatus = ActionStatus.SENT,
    ) -> None:
        self.requests: list[ActionRequest] = []
        self._on_reply = on_reply
        self._reply_status = reply_status

    async def dispatch(self, request: ActionRequest) -> ActionResult:
        self.requests.append(request)
        status = ActionStatus.SENT
        if request.type is ActionType.REPLY:
            if self._on_reply is not None:
                await self._on_reply(request)
            status = self._reply_status
        return ActionResult(status=status, action_id=uuid.uuid4(), detail="fake")

    def of(self, action: ActionType) -> list[ActionRequest]:
        return [request for request in self.requests if request.type is action]


class FakeDatabase:
    """Сессия, в которой MessageRepository.set_status меняет строку в памяти."""

    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, SimpleNamespace] = {}

    def add_message(self) -> uuid.UUID:
        message_id = uuid.uuid4()
        self.rows[message_id] = SimpleNamespace(
            processed_status=ProcessedStatus.MATCHED, rule_id=None, status_reason=None
        )
        return message_id

    @contextlib.asynccontextmanager
    async def session(self) -> AsyncIterator[Any]:
        yield self

    async def get(self, _model: object, ident: uuid.UUID) -> SimpleNamespace | None:
        return self.rows.get(ident)

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


def make_env(sleep: GatedSleep | None = None, actions: FakeActions | None = None) -> Env:
    cooldown = FakeCooldown()
    database = FakeDatabase()
    sleep = sleep or GatedSleep()
    actions = actions or FakeActions()
    pipeline = ReplyPipeline(
        make_settings(anti_duplicate_ttl_seconds=0),
        database,  # type: ignore[arg-type]
        analyzer=None,
        generator=None,
        context=None,  # type: ignore[arg-type]
        validator=ReplyValidator(),
        cooldown=cooldown,  # type: ignore[arg-type]
        actions=actions,  # type: ignore[arg-type]
        sleep=sleep,
    )
    return Env(pipeline, cooldown, actions, database, sleep)


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
    tg_message_id: int = 1, sender: int = SENDER, chat_type: ChatType = ChatType.SUPERGROUP
) -> NormalizedMessage:
    return NormalizedMessage(
        account_id=ACCOUNT,
        tg_chat_id=TG_CHAT,
        tg_message_id=tg_message_id,
        chat_type=chat_type,
        text="нужен дизайнер",
        date=datetime.now(UTC),
        is_incoming=True,
        is_outgoing=False,
        sender_tg_id=sender,
    )


@dataclass
class Sent:
    outcome: ReplyOutcome
    message_id: uuid.UUID


async def send(
    env: Env,
    scenario: Scenario,
    *,
    message: NormalizedMessage | None = None,
    match: RuleMatch | None = None,
    one_shot_peer: int | None = None,
    review: bool = False,
) -> Sent:
    """Ответ, прошедший анализ и генерацию: вход в стадию отправки."""
    message = message or make_message()
    match = match or make_match()
    message_id = env.database.add_message()
    outcome = await env.pipeline._send(
        message,
        match,
        None,
        message_id,
        PromptContext(),
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
    )
    return Sent(outcome, message_id)


async def drain(env: Env) -> None:
    await asyncio.wait_for(asyncio.gather(*list(env.pipeline._delayed)), timeout=WAIT)


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
        scenario = make_scenario(reply_delay_min_seconds=15, reply_delay_max_seconds=None)
        assert reply_delay_seconds(scenario) == 15.0

    def test_only_max_means_from_zero(self) -> None:
        scenario = make_scenario(reply_delay_min_seconds=None, reply_delay_max_seconds=30)
        assert all(0 <= reply_delay_seconds(scenario) <= 30 for _ in range(50))


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
        assert "задержка сценария" in row.status_reason

        env.sleep.release.set()
        await drain(env)

        assert len(env.actions.of(ActionType.REPLY)) == 1
        assert row.processed_status is ProcessedStatus.REPLIED
        assert not env.pipeline._delayed

    @pytest.mark.parametrize(("low", "high"), [(None, None), (0, 0), (0, None), (None, 0)])
    async def test_no_wait_when_delay_is_unset(self, low: int | None, high: int | None) -> None:
        env = make_env()
        scenario = make_scenario(reply_delay_min_seconds=low, reply_delay_max_seconds=high)

        sent = await send(env, scenario)

        assert env.sleep.calls == []
        assert sent.outcome.action is ActionType.REPLY
        assert sent.outcome.status is ActionStatus.SENT
        assert len(env.actions.of(ActionType.REPLY)) == 1
        assert not env.pipeline._delayed

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
        scenario = make_scenario(reply_delay_min_seconds=30, reply_delay_max_seconds=30)
        match = make_match(CooldownSpec(user=600))

        await send(env, scenario, match=match)
        await asyncio.wait_for(env.sleep.started.wait(), timeout=WAIT)
        second = await send(env, scenario, match=match, message=make_message(2))

        assert second.outcome.action is ActionType.IGNORE
        assert "cooldown" in (second.outcome.reason or "")

        env.sleep.release.set()
        await drain(env)
        assert len(env.actions.of(ActionType.REPLY)) == 1

    async def test_one_shot_mark_blocks_a_second_reply_during_the_wait(self) -> None:
        """Лид заводится только на отправке — до неё one_shot держит метка."""
        env = make_env()
        scenario = make_scenario(
            one_shot=True, reply_delay_min_seconds=30, reply_delay_max_seconds=30
        )
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
        scenario = make_scenario(
            reply_in_dm=True,
            group_ack_text="Отправлю в лс",
            reply_delay_min_seconds=3,
            reply_delay_max_seconds=3,
        )

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
        scenario = make_scenario(reply_delay_min_seconds=30, reply_delay_max_seconds=30)

        sent = await send(env, scenario, review=True)

        assert sent.outcome.action is ActionType.REQUEST_REVIEW
        assert env.sleep.calls == []
        assert not env.pipeline._delayed

    async def test_failed_send_after_the_wait_releases_cooldown_and_escalates(self) -> None:
        env = make_env(actions=FakeActions(reply_status=ActionStatus.FAILED))
        sent = await send(env, make_scenario(reply_delay_min_seconds=1, reply_delay_max_seconds=1))

        env.sleep.release.set()
        await drain(env)

        assert env.cooldown.held == set()
        assert len(env.actions.of(ActionType.ESCALATE_TO_HUMAN)) == 1
        assert env.database.rows[sent.message_id].processed_status is ProcessedStatus.ESCALATED

    async def test_crash_during_delayed_send_is_caught_and_escalated(self) -> None:
        async def boom(_request: ActionRequest) -> None:
            raise RuntimeError("db is down")

        env = make_env(actions=FakeActions(on_reply=boom))
        sent = await send(env, make_scenario(reply_delay_min_seconds=1, reply_delay_max_seconds=1))

        env.sleep.release.set()
        await drain(env)  # задача не падает наружу

        escalations = env.actions.of(ActionType.ESCALATE_TO_HUMAN)
        assert len(escalations) == 1
        assert "db is down" in escalations[0].payload["reason"]
        # Отправка уже начиналась — ответ мог уйти, кулдаун не снимаем.
        assert env.cooldown.held
        assert env.database.rows[sent.message_id].processed_status is ProcessedStatus.ESCALATED


class TestShutdown:
    async def test_cancels_the_wait_releases_cooldown_and_escalates(self) -> None:
        env = make_env()
        scenario = make_scenario(
            one_shot=True, reply_delay_min_seconds=600, reply_delay_max_seconds=600
        )
        sent = await send(env, scenario, one_shot_peer=SENDER)
        await asyncio.wait_for(env.sleep.started.wait(), timeout=WAIT)

        await env.pipeline.shutdown(grace_seconds=WAIT)

        assert env.actions.of(ActionType.REPLY) == []
        escalations = env.actions.of(ActionType.ESCALATE_TO_HUMAN)
        assert len(escalations) == 1
        assert "остановка воркера" in escalations[0].payload["reason"]
        assert env.cooldown.held == set(), "ни кулдаун, ни метка one_shot не висят"
        assert env.database.rows[sent.message_id].processed_status is ProcessedStatus.ESCALATED
        assert not env.pipeline._delayed

    async def test_no_new_delayed_replies_after_shutdown(self) -> None:
        env = make_env()
        await env.pipeline.shutdown()

        sent = await send(env, make_scenario(reply_delay_min_seconds=5, reply_delay_max_seconds=5))

        assert sent.outcome.action is ActionType.ESCALATE_TO_HUMAN
        assert env.sleep.calls == []
        assert env.actions.of(ActionType.REPLY) == []
        assert env.cooldown.held == set()
        assert not env.pipeline._delayed
