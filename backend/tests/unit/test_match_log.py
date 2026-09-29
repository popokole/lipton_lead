"""Карточки совпадений в лог-чате: текст, итог, лимит Telegram, очередь.

Карточка вторична по отношению к ответу лиду: конвейер ставит задание в
очередь синхронно и никогда не ждёт Bot API. Поэтому здесь проверяется и
текст (экранирование, обрезка, лимит 4096), и то, что лимиты, 429 и
переполнение очереди не теряют итог молча.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import inspect
import json
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.api.v1 import notify as notify_api
from app.core.crypto import build_secret_box
from app.models import ActionStatus, ActionType, ChatType, Rule, RuleScope
from app.models.notify import SINGLETON_ID, NotifySettings
from app.notifications.match_log import (
    MAX_INCOMING_CHARS,
    PENDING_LINE,
    TELEGRAM_TEXT_LIMIT,
    CardState,
    ChatRateLimiter,
    MatchCard,
    MatchLogReporter,
    clip,
    crash_line,
    format_match_card,
    humanize_reason,
    outcome_line,
)
from app.notifications.notifier import (
    LogTarget,
    NotifierBot,
    NotifyError,
    NotifyRateLimitError,
)
from app.pipeline.reply_pipeline import ReplyOutcome
from app.rules.engine import CompiledRule, CooldownSpec, RuleMatch
from app.rules.filters import MessageFilterSpec
from app.rules.keywords import KeywordSpec
from app.telegram.messages import NormalizedMessage
from tests.conftest import make_settings

ACCOUNT = uuid.uuid4()
RULE_ID = uuid.uuid4()
GROUP = -100_777
THREAD = 55
PREFIXES = ("✅", "❌", "⏭", "🟡", "⏳")


# --- сборка ------------------------------------------------------------------
def make_message(text: str = "нужен дизайнер", **fields: Any) -> NormalizedMessage:
    defaults: dict[str, Any] = {
        "account_id": ACCOUNT,
        "tg_chat_id": -100_123_456,
        "tg_message_id": 42,
        "chat_type": ChatType.SUPERGROUP,
        "text": text,
        "date": datetime(2026, 9, 29, 9, 5, 7, tzinfo=UTC),
        "is_incoming": True,
        "is_outgoing": False,
        "sender_tg_id": 4242,
        "sender_username": "ivan",
        "sender_display_name": "Иван",
        "chat_title": "Дизайн-чат",
    }
    return NormalizedMessage(**{**defaults, **fields})


def make_match(name: str = "Поиск клиентов") -> RuleMatch:
    return RuleMatch(
        rule=CompiledRule(
            id=RULE_ID,
            name=name,
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
            cooldown=CooldownSpec(),
        )
    )


def card_text(message: NormalizedMessage | None = None, **fields: Any) -> str:
    message = message or make_message()
    params: dict[str, Any] = {
        "rule_name": "Поиск клиентов",
        "account_label": "Продажи-1",
        "chat_title": message.chat_title,
        "chat_username": message.chat_username,
        "tg_chat_id": message.tg_chat_id,
        "tg_message_id": message.tg_message_id,
        "is_private": message.is_private,
        "sender_name": message.sender_display_name,
        "sender_username": message.sender_username,
        "sender_tg_id": message.sender_tg_id,
        "incoming_text": message.text,
        "date": message.date,
        "tz_offset_hours": 3,
    }
    return format_match_card(**{**params, **fields})


def visible(text: str) -> str:
    """Текст так, как его считает Telegram: без тегов и сущностей."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def outcome(action: ActionType, status: ActionStatus, **fields: Any) -> ReplyOutcome:
    return ReplyOutcome(action=action, status=status, **fields)


def analysis(confidence: float) -> Any:
    return SimpleNamespace(failed=False, result=SimpleNamespace(confidence=confidence))


# --- текст карточки ----------------------------------------------------------------
class TestCardText:
    def test_has_rule_account_chat_sender_text_and_local_time(self) -> None:
        text = card_text()

        assert "Поиск клиентов" in text
        assert "аккаунт Продажи-1" in text
        assert "Дизайн-чат" in text
        # Приватная супергруппа: ссылка на сообщение через /c/.
        assert 'href="https://t.me/c/123456/42"' in text
        assert "Иван" in text and "@ivan" in text
        assert "нужен дизайнер" in text
        assert "29.09 12:05:07" in text  # 09:05 UTC + 3 часа
        assert text.endswith(PENDING_LINE), "итог ещё не известен"

    def test_result_replaces_the_pending_line(self) -> None:
        text = card_text(result="✅ Ответ отправлен")
        assert text.endswith("✅ Ответ отправлен")
        assert PENDING_LINE not in text

    def test_public_chat_links_by_username(self) -> None:
        text = card_text(make_message(chat_username="designchat"))
        assert 'href="https://t.me/designchat/42"' in text

    def test_private_chat_has_no_message_link(self) -> None:
        text = card_text(make_message(chat_type=ChatType.PRIVATE, chat_title=None))
        assert "💬 личка" in text
        assert "t.me/" not in text

    def test_sender_without_username_gets_a_tg_user_link(self) -> None:
        text = card_text(make_message(sender_username=None))
        assert '<a href="tg://user?id=4242">написать</a>' in text

    def test_sender_known_only_by_username_is_not_repeated(self) -> None:
        text = card_text(make_message(sender_display_name=None))
        assert "👤 @ivan\n" in text

    def test_empty_text_is_marked(self) -> None:
        assert "(без текста)" in card_text(make_message(text="   "))

    def test_escapes_html_in_every_user_field(self) -> None:
        message = make_message(
            text="<a href='https://evil'>жми</a> & <b>всё</b>",
            chat_title="A & B <script>",
            sender_display_name="<Злой>",
            sender_username=None,
        )
        text = card_text(message, rule_name="<i>правило</i>", account_label="<u>акк</u>")

        assert "<script>" not in text and "&lt;script&gt;" in text
        assert "&lt;a href='https://evil'&gt;" in text
        assert "&amp; &lt;b&gt;всё&lt;/b&gt;" in text
        assert "&lt;Злой&gt;" in text
        assert "&lt;i&gt;правило&lt;/i&gt;" in text
        assert "&lt;u&gt;акк&lt;/u&gt;" in text
        # Живые теги — только наши: жирный заголовок и ссылки.
        assert set(re.findall(r"</?(\w+)", text)) <= {"b", "a"}
        assert "evil" not in re.findall(r'href="([^"]+)"', text)[0]

    def test_long_text_is_clipped_with_an_ellipsis(self) -> None:
        text = card_text(make_message(text="а" * 5000))
        body = text.split("<b>Сообщение:</b>\n", 1)[1].split("\n", 1)[0]
        assert body.endswith("…")
        assert utf16(body) == MAX_INCOMING_CHARS

    def test_clip_happens_before_escaping(self) -> None:
        """Обрезка после экранирования разрезала бы «&amp;» пополам."""
        text = card_text(make_message(text="&" * 2000))
        assert re.findall(r"&(?!amp;|lt;|gt;|quot;|#)", text) == []

    def test_worst_case_card_fits_the_telegram_limit(self) -> None:
        huge = "😀" * 5000  # эмодзи — две единицы UTF-16 каждый
        message = make_message(
            text=huge, chat_title=huge, sender_display_name=huge, sender_username=None
        )
        result = outcome_line(
            outcome(
                ActionType.REPLY,
                ActionStatus.SENT,
                reply_text=huge,
                dm_text=huge,
                analysis=analysis(0.9),
                delay_seconds=3600,
            )
        )
        text = card_text(message, rule_name=huge, account_label=huge, result=result)
        assert utf16(visible(text)) < TELEGRAM_TEXT_LIMIT

    def test_clip_never_splits_an_emoji(self) -> None:
        assert clip("😀" * 10, 5) == "😀😀…"
        assert clip("короткий", 50) == "короткий"
        assert utf16(clip("😀" * 10, 6)) <= 6


# --- итог → строка карточки ------------------------------------------------------
class TestOutcomeLine:
    @pytest.mark.parametrize("action", list(ActionType))
    @pytest.mark.parametrize("status", list(ActionStatus))
    def test_every_action_and_status_has_a_line(
        self, action: ActionType, status: ActionStatus
    ) -> None:
        line = outcome_line(outcome(action, status, reason="сбой <x> & y"))
        assert line.startswith(PREFIXES)
        assert "<x>" not in line, "причина экранируется"

    def test_sent_reply_shows_the_text(self) -> None:
        line = outcome_line(
            outcome(
                ActionType.REPLY,
                ActionStatus.SENT,
                reply_text="Здравствуйте! <b>Скидка</b>",
                analysis=analysis(0.82),
            )
        )
        assert line.startswith("✅ Ответ отправлен · уверенность ИИ 0.82")
        assert "<b>Наш ответ:</b>" in line
        assert "Здравствуйте! &lt;b&gt;Скидка&lt;/b&gt;" in line

    def test_sent_reply_in_chat_and_dm(self) -> None:
        line = outcome_line(
            outcome(
                ActionType.REPLY,
                ActionStatus.SENT,
                reply_text="Отправлю в лс",
                dm_text="Развёрнутый ответ",
            )
        )
        assert "<b>В чат:</b>\nОтправлю в лс" in line
        assert "<b>В личку:</b>\nРазвёрнутый ответ" in line

    def test_failed_dm_is_shown(self) -> None:
        line = outcome_line(
            outcome(
                ActionType.REPLY,
                ActionStatus.SENT,
                reply_text="Отправлю в лс",
                dm_text="Развёрнутый ответ",
                dm_error="PrivacyRestricted",
            )
        )
        assert line.startswith("✅ Ответ отправлен")
        assert "⚠️ Личка не ушла: PrivacyRestricted" in line
        assert "Развёрнутый ответ" not in line

    def test_delayed_and_duplicate_marks(self) -> None:
        line = outcome_line(
            outcome(ActionType.REPLY, ActionStatus.SENT, reason="duplicate", delay_seconds=30.4)
        )
        assert "(после паузы 30 с)" in line
        assert "повтор" in line

    def test_scheduled_reply(self) -> None:
        line = outcome_line(outcome(ActionType.REPLY, ActionStatus.PENDING, delay_seconds=30.4))
        assert line == "⏳ Ответ запланирован через 30 с"

    @pytest.mark.parametrize(
        "status", [ActionStatus.FAILED, ActionStatus.REJECTED, ActionStatus.CANCELLED]
    )
    def test_reply_not_sent(self, status: ActionStatus) -> None:
        line = outcome_line(outcome(ActionType.REPLY, status, reason="ответ пустой"))
        assert line == "❌ Не удалось отправить: ответ пустой"

    def test_send_failure_escalated_to_operator(self) -> None:
        line = outcome_line(
            outcome(
                ActionType.ESCALATE_TO_HUMAN,
                ActionStatus.SENT,
                reason="не удалось отправить ответ: Telegram просит подождать 30с",
                send_error="Telegram просит подождать 30с",
            )
        )
        assert line == "❌ Не удалось отправить: Telegram просит подождать 30с · передано оператору"

    def test_review_mentions_the_operator(self) -> None:
        line = outcome_line(
            outcome(
                ActionType.REQUEST_REVIEW,
                ActionStatus.SENT,
                reason="на подтверждении оператора",
                analysis=analysis(0.55),
            )
        )
        assert line.startswith("🟡 Не отправляли: ответ на проверке у оператора")
        assert "уверенность ИИ 0.55" in line

    def test_review_that_failed(self) -> None:
        line = outcome_line(
            outcome(ActionType.REQUEST_REVIEW, ActionStatus.REJECTED, reason="нечего подтверждать")
        )
        assert line.startswith("❌ Не удалось отдать ответ на проверку оператору")

    @pytest.mark.parametrize(
        ("reason", "expected"),
        [
            ("cooldown: user", "кулдаун — этому человеку недавно уже отвечали"),
            ("cooldown: chat", "кулдаун — в этот чат недавно уже отвечали"),
            ("cooldown: account", "кулдаун — аккаунт недавно уже отвечал"),
            ("cooldown: rule", "кулдаун — правило недавно уже срабатывало"),
            ("cooldown: scenario", "кулдаун — сценарий недавно уже отвечал"),
            ("анти-бан: лимит на чат", "анти-бан: в этот чат недавно уже отвечали"),
            ("вне рабочих часов", "вне рабочих часов"),
            (
                "AI: не отвечать (confidence 0.30, порог 0.70)",
                "ИИ решил не отвечать (уверенность 0.30 при пороге 0.70)",
            ),
            ("one_shot: уже связались", "«один заход»: с этим человеком уже связывались"),
            (
                "one_shot: ответ уже запланирован",
                "«один заход»: ответ этому человеку уже запланирован",
            ),
            (
                "стоп-лист: отправитель добавлен во время паузы",
                "стоп-лист: отправитель добавлен во время паузы",
            ),
        ],
    )
    def test_ignore_reasons_are_human_readable(self, reason: str, expected: str) -> None:
        assert humanize_reason(reason) == expected
        line = outcome_line(outcome(ActionType.IGNORE, ActionStatus.SENT, reason=reason))
        assert line == f"⏭ Не отправляли: {html.escape(expected, quote=False)}"

    def test_ignore_whose_journal_write_failed(self) -> None:
        line = outcome_line(
            outcome(ActionType.IGNORE, ActionStatus.FAILED, reason="вне рабочих часов")
        )
        assert line.startswith("⏭ Не отправляли: вне рабочих часов")
        assert "FAILED" in line

    @pytest.mark.parametrize(
        "reason",
        [
            "низкая уверенность AI (0.30 < 0.70)",
            "модель просит передать человеку",
            "AI не настроен, нужен оператор",
            "нет сценария для ответа",
            "отложенный ответ отменён: остановка воркера",
            "воркер останавливается — отложенный ответ не отправлен",
        ],
    )
    def test_escalations_are_handed_to_the_operator(self, reason: str) -> None:
        line = outcome_line(outcome(ActionType.ESCALATE_TO_HUMAN, ActionStatus.SENT, reason=reason))
        assert line == (f"⏭ Не отправляли: {html.escape(reason, quote=False)} · передано оператору")

    def test_escalation_that_failed(self) -> None:
        line = outcome_line(
            outcome(ActionType.ESCALATE_TO_HUMAN, ActionStatus.FAILED, reason="нужен оператор")
        )
        assert "передать оператору не удалось (FAILED)" in line

    @pytest.mark.parametrize(
        ("action", "label"),
        [
            (ActionType.NOTIFY_ADMIN, "уведомление в панель"),
            (ActionType.SAVE_LEAD, "сохранить лида"),
            (ActionType.TAG_USER, "метка собеседнику"),
        ],
    )
    def test_rules_without_a_reply(self, action: ActionType, label: str) -> None:
        done = outcome_line(outcome(action, ActionStatus.SENT, reason="HOT (80)"))
        assert done == f"⏭ Не отправляли: правило без ответа — {label}: выполнено (HOT (80))"
        failed = outcome_line(outcome(action, ActionStatus.REJECTED, reason="нужны key и value"))
        assert failed == f"❌ Не удалось: {label} — нужны key и value"

    def test_crash_line_is_escaped(self) -> None:
        line = crash_line(RuntimeError("<boom>"))
        assert line == "❌ Не удалось отправить: обработка упала — RuntimeError: &lt;boom&gt;"


# --- лимит Telegram ----------------------------------------------------------------
class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 3))
        self.now += seconds
        await asyncio.sleep(0)


class TestChatRateLimiter:
    def _limiter(self, clock: FakeClock, **kwargs: Any) -> ChatRateLimiter:
        return ChatRateLimiter(clock=clock, sleep=clock.sleep, **kwargs)

    async def test_not_more_often_than_the_min_interval(self) -> None:
        clock = FakeClock()
        limiter = self._limiter(clock, min_interval_seconds=1.0)
        await limiter.acquire(GROUP)
        await limiter.acquire(GROUP)
        assert clock.sleeps == [1.0]

    async def test_window_cap_waits_for_the_oldest_to_expire(self) -> None:
        clock = FakeClock()
        limiter = self._limiter(clock, max_per_window=3, window_seconds=60, min_interval_seconds=0)
        for _ in range(3):
            await limiter.acquire(GROUP)
        assert clock.sleeps == []
        await limiter.acquire(GROUP)
        assert clock.sleeps == [60.0]

    async def test_block_after_429_holds_the_chat(self) -> None:
        clock = FakeClock()
        limiter = self._limiter(clock, min_interval_seconds=0)
        limiter.block(GROUP, 7)
        assert limiter.delay(GROUP) == 7
        assert limiter.delay(GROUP + 1) == 0, "другие чаты не ждут"
        await limiter.acquire(GROUP)
        assert clock.sleeps == [7.0]

    async def test_default_stays_below_telegram_group_limit(self) -> None:
        clock = FakeClock()
        limiter = self._limiter(clock, min_interval_seconds=0)
        for _ in range(18):
            await limiter.acquire(GROUP)
        assert clock.sleeps == []
        await limiter.acquire(GROUP)
        assert clock.sleeps, "19-я за минуту ждёт"


# --- очередь и отправка --------------------------------------------------------------
class FakeNotifier:
    """Bot API в памяти: записывает вызовы, умеет падать и тормозить."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.enabled = True
        self.fail_send: list[Exception] = []
        self.fail_edit: list[Exception] = []
        self.gate: asyncio.Event | None = None
        self.errors: list[str] = []
        self._next_id = 100

    async def log_target(self, _db: Any, *, rule_id: uuid.UUID | None) -> LogTarget | None:
        self.calls.append(("target", rule_id))
        return LogTarget(token="t", group_id=GROUP, thread_id=THREAD) if self.enabled else None

    async def send_log_card(
        self, target: LogTarget, text: str, *, reply_to: int | None = None
    ) -> int:
        if self.gate is not None:
            await self.gate.wait()
        self.calls.append(("send", text, reply_to))
        if self.fail_send:
            raise self.fail_send.pop(0)
        self._next_id += 1
        return self._next_id

    async def edit_log_card(self, target: LogTarget, message_id: int, text: str) -> None:
        self.calls.append(("edit", message_id, text))
        if self.fail_edit:
            raise self.fail_edit.pop(0)

    async def record_error(self, _db: Any, detail: str) -> None:
        self.errors.append(detail)

    def of(self, kind: str) -> list[tuple[Any, ...]]:
        return [call for call in self.calls if call[0] == kind]


class FakeDatabase:
    @contextlib.asynccontextmanager
    async def session(self) -> AsyncIterator[Any]:
        yield self

    async def get(self, _model: Any, _ident: Any) -> Any:
        return SimpleNamespace(label="Продажи-1", username=None)


_REPORTERS: list[MatchLogReporter] = []


@pytest.fixture(autouse=True)
async def _stop_reporters() -> AsyncIterator[None]:
    """Гасит фоновые задачи очередей после каждого теста."""
    yield
    while _REPORTERS:
        reporter = _REPORTERS.pop()
        if reporter._task is not None and not reporter._task.done():
            reporter._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reporter._task


def make_reporter(
    notifier: FakeNotifier, *, queue_limit: int = 200
) -> tuple[MatchLogReporter, FakeClock]:
    clock = FakeClock()
    reporter = MatchLogReporter(
        FakeDatabase(),  # type: ignore[arg-type]
        notifier,  # type: ignore[arg-type]
        tz_offset_hours=3,
        queue_limit=queue_limit,
        limiter=ChatRateLimiter(clock=clock, sleep=clock.sleep),
    )
    _REPORTERS.append(reporter)
    return reporter, clock


async def idle(reporter: MatchLogReporter) -> None:
    """Ждёт, пока фоновая задача разберёт очередь и уснёт."""
    for _ in range(500):
        await asyncio.sleep(0)
        if not reporter.pending and not reporter._wakeup.is_set():
            return
    raise AssertionError("очередь не разобрана")


def open_card(reporter: MatchLogReporter, text: str = "нужен дизайнер") -> MatchCard:
    card = reporter.open_card(make_message(text), make_match())
    assert card is not None
    return card


SENT = outcome(ActionType.REPLY, ActionStatus.SENT, reply_text="Здравствуйте!")
IGNORED = outcome(ActionType.IGNORE, ActionStatus.SENT, reason="cooldown: user")
SCHEDULED = outcome(ActionType.REPLY, ActionStatus.PENDING, delay_seconds=30)


class TestReporter:
    async def test_result_known_before_posting_goes_out_in_one_message(self) -> None:
        notifier = FakeNotifier()
        reporter, _ = make_reporter(notifier)

        card = open_card(reporter)
        card.report(IGNORED)
        await idle(reporter)

        sends = notifier.of("send")
        assert len(sends) == 1
        assert "⏭ Не отправляли: кулдаун" in sends[0][1]
        assert notifier.of("edit") == []
        assert notifier.of("target") == [("target", RULE_ID)]

    async def test_card_goes_out_first_then_is_edited_with_the_result(self) -> None:
        notifier = FakeNotifier()
        reporter, _ = make_reporter(notifier)

        card = open_card(reporter)
        await idle(reporter)
        assert notifier.of("send")[0][1].endswith(PENDING_LINE)

        card.report(SENT)
        await idle(reporter)

        edits = notifier.of("edit")
        assert len(edits) == 1
        _, message_id, text = edits[0]
        assert message_id == card.message_id == 101
        assert "✅ Ответ отправлен" in text
        assert "нужен дизайнер" in text, "карточка переписана целиком"
        assert len(notifier.of("send")) == 1, "без второй карточки"

    async def test_scheduled_then_final_result(self) -> None:
        notifier = FakeNotifier()
        reporter, _ = make_reporter(notifier)
        card = open_card(reporter)
        await idle(reporter)

        card.report(SCHEDULED)
        await idle(reporter)
        assert "⏳ Ответ запланирован через 30 с" in notifier.of("edit")[-1][2]

        card.report(SENT)
        await idle(reporter)
        final = notifier.of("edit")[-1][2]
        assert "✅ Ответ отправлен" in final and "⏳" not in final

        # Запоздалое «⏳» итог не перетирает.
        card.report(SCHEDULED)
        await idle(reporter)
        assert len(notifier.of("edit")) == 2

    async def test_same_result_is_not_edited_twice(self) -> None:
        notifier = FakeNotifier()
        reporter, _ = make_reporter(notifier)
        card = open_card(reporter)
        await idle(reporter)

        card.report(SENT)
        card.report(SENT)
        await idle(reporter)
        card.report(SENT)
        await idle(reporter)

        assert len(notifier.of("edit")) == 1

    async def test_result_arriving_while_posting_is_edited_after(self) -> None:
        notifier = FakeNotifier()
        notifier.gate = asyncio.Event()
        reporter, _ = make_reporter(notifier)
        card = open_card(reporter)
        for _ in range(10):
            await asyncio.sleep(0)
        assert card.state is CardState.POSTING

        card.report(SENT)
        notifier.gate.set()
        await idle(reporter)

        assert notifier.of("send")[0][1].endswith(PENDING_LINE)
        assert "✅ Ответ отправлен" in notifier.of("edit")[0][2]

    async def test_429_waits_retry_after_and_retries_once(self) -> None:
        notifier = FakeNotifier()
        notifier.fail_send = [NotifyRateLimitError("Too Many Requests", retry_after=7)]
        reporter, clock = make_reporter(notifier)

        card = open_card(reporter)
        card.report(SENT)
        await idle(reporter)

        assert len(notifier.of("send")) == 2, "одна повторная попытка"
        assert 7.0 in clock.sleeps, "ждали столько, сколько сказал Telegram"
        assert card.state is CardState.POSTED
        assert reporter.throttled == 1

    async def test_429_twice_is_reported_and_the_result_retried_later(self) -> None:
        notifier = FakeNotifier()
        notifier.fail_send = [
            NotifyRateLimitError("Too Many Requests", retry_after=3),
            NotifyRateLimitError("Too Many Requests", retry_after=3),
        ]
        reporter, _ = make_reporter(notifier)

        card = open_card(reporter)
        await idle(reporter)
        assert card.state is CardState.FAILED
        assert notifier.errors, "ошибка видна в настройках бота"
        assert reporter.failed == 1

        # Итог пришёл позже — карточка уходит целиком заново, с итогом.
        card.report(SENT)
        await idle(reporter)
        sends = notifier.of("send")
        assert len(sends) == 3
        assert "✅ Ответ отправлен" in sends[-1][1]
        assert card.state is CardState.POSTED

    async def test_edit_failure_falls_back_to_a_reply_in_the_thread(self) -> None:
        notifier = FakeNotifier()
        reporter, _ = make_reporter(notifier)
        card = open_card(reporter)
        await idle(reporter)

        notifier.fail_edit = [NotifyError("Bad Request: message to edit not found")]
        card.report(SCHEDULED)
        await idle(reporter)

        assert len(notifier.of("edit")) == 1
        reply = notifier.of("send")[-1]
        assert reply == ("send", "⏳ Ответ запланирован через 30 с", card.message_id)

        # Дальше итог сразу ответом, без заведомо неудачной правки.
        card.report(SENT)
        await idle(reporter)
        assert len(notifier.of("edit")) == 1
        assert notifier.of("send")[-1][2] == card.message_id
        assert notifier.of("send")[-1][1].startswith("✅ Ответ отправлен")

    async def test_nothing_is_sent_when_turned_off(self) -> None:
        notifier = FakeNotifier()
        notifier.enabled = False
        reporter, _ = make_reporter(notifier)

        card = open_card(reporter)
        await idle(reporter)
        card.report(SENT)
        await idle(reporter)

        assert card.state is CardState.SKIPPED
        assert notifier.of("send") == [] and notifier.of("edit") == []

    async def test_overflow_drops_the_oldest_cards_and_counts_them(self) -> None:
        notifier = FakeNotifier()
        reporter, _ = make_reporter(notifier, queue_limit=3)

        cards = [open_card(reporter, f"сообщение {index}") for index in range(5)]
        assert reporter.dropped_cards == 2
        assert [card.state for card in cards[:2]] == [CardState.DROPPED] * 2

        cards[0].report(SENT)  # итог выброшенной карточки — без ошибок и отправок
        await idle(reporter)

        texts = [call[1] for call in notifier.of("send")]
        assert len(texts) == 3
        assert all(
            f"сообщение {index}" in text for index, text in zip(range(2, 5), texts, strict=True)
        )

    async def test_overflow_of_updates_drops_the_oldest_update(self) -> None:
        notifier = FakeNotifier()
        reporter, _ = make_reporter(notifier, queue_limit=2)
        cards = [open_card(reporter, f"m{index}") for index in range(2)]
        await idle(reporter)

        notifier.gate = asyncio.Event()  # правки встанут в очередь
        for card in cards:
            card.report(SENT)
        newest = open_card(reporter, "m2")
        assert reporter.dropped_updates == 1
        assert newest.state is CardState.QUEUED

    async def test_pipeline_never_waits_for_a_slow_bot(self) -> None:
        assert not inspect.iscoroutinefunction(MatchLogReporter.open_card)
        assert not inspect.iscoroutinefunction(MatchCard.report)
        assert not inspect.iscoroutinefunction(MatchCard.report_line)

        notifier = FakeNotifier()
        notifier.gate = asyncio.Event()  # Bot API «висит»
        reporter, _ = make_reporter(notifier)
        first = open_card(reporter)
        for _ in range(10):
            await asyncio.sleep(0)
        assert first.state is CardState.POSTING

        # Пока бот висит, новые совпадения и итоги проходят мгновенно.
        second = open_card(reporter, "второе")
        second.report(SENT)
        first.report(IGNORED)
        assert reporter.pending == 2

        notifier.gate.set()
        await idle(reporter)
        assert len(notifier.of("send")) == 2

    async def test_close_flushes_the_queue_and_stops_accepting(self) -> None:
        notifier = FakeNotifier()
        reporter, _ = make_reporter(notifier)
        open_card(reporter).report(SENT)
        open_card(reporter, "второе").report(IGNORED)

        await reporter.close(grace_seconds=1.0)

        assert len(notifier.of("send")) == 2
        assert reporter.open_card(make_message(), make_match()) is None

    async def test_close_gives_up_after_the_grace_period(self) -> None:
        notifier = FakeNotifier()
        notifier.gate = asyncio.Event()  # не отпустим никогда
        reporter, _ = make_reporter(notifier)
        open_card(reporter)
        open_card(reporter, "второе")

        await asyncio.wait_for(reporter.close(grace_seconds=0.05), timeout=1.0)

        assert reporter._task is not None and reporter._task.done()

    async def test_reporting_never_raises(self) -> None:
        notifier = FakeNotifier()
        reporter, _ = make_reporter(notifier)
        card = open_card(reporter)
        broken = SimpleNamespace(action=ActionType.REPLY, status=ActionStatus.SENT, scheduled=False)

        card.report(broken)  # type: ignore[arg-type]
        await idle(reporter)

        assert "❓ Итог" in notifier.of("send")[0][1]


# --- NotifierBot: Bot API ---------------------------------------------------------
def make_bot(handler: Any) -> NotifierBot:
    bot = NotifierBot(build_secret_box(make_settings()))
    bot._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return bot


TARGET = LogTarget(token="123:abc", group_id=GROUP, thread_id=THREAD)


class TestNotifierBotLogCards:
    async def test_429_raises_with_retry_after(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                429,
                json={
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests: retry after 7",
                    "parameters": {"retry_after": 7},
                },
            )

        bot = make_bot(handler)
        with pytest.raises(NotifyRateLimitError) as error:
            await bot.send_log_card(TARGET, "текст")
        assert error.value.retry_after == 7

    async def test_other_errors_are_plain_notify_errors(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400, json={"ok": False, "error_code": 400, "description": "Bad Request"}
            )

        bot = make_bot(handler)
        with pytest.raises(NotifyError) as error:
            await bot.send_log_card(TARGET, "текст")
        assert not isinstance(error.value, NotifyRateLimitError)

    async def test_send_goes_to_the_thread_and_can_reply(self) -> None:
        seen: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 9}})

        bot = make_bot(handler)
        assert await bot.send_log_card(TARGET, "карточка") == 9
        assert await bot.send_log_card(TARGET, "итог", reply_to=9) == 9

        assert seen[0]["chat_id"] == GROUP
        assert seen[0]["message_thread_id"] == THREAD
        assert seen[0]["parse_mode"] == "HTML"
        assert "reply_to_message_id" not in seen[0]
        assert seen[1]["reply_to_message_id"] == 9
        assert seen[1]["allow_sending_without_reply"] is True

    async def test_edit_not_modified_is_not_an_error(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={
                    "ok": False,
                    "error_code": 400,
                    "description": "Bad Request: message is not modified",
                },
            )

        await make_bot(handler).edit_log_card(TARGET, 9, "то же самое")


class FakeSession:
    def __init__(self, rows: dict[tuple[Any, Any], Any]) -> None:
        self.rows = rows

    async def get(self, model: Any, ident: Any) -> Any:
        return self.rows.get((model, ident))

    async def flush(self) -> None:
        return None

    async def execute(self, _statement: Any) -> None:
        return None


class TestLogTarget:
    def _settings_row(self, **fields: Any) -> NotifySettings:
        blob = build_secret_box(make_settings()).encrypt("123:abc", aad="notify")
        defaults: dict[str, Any] = {
            "id": SINGLETON_ID,
            "enabled": True,
            "group_id": GROUP,
            "bot_token_ct": blob.ciphertext,
            "bot_token_nonce": blob.nonce,
            "bot_token_key_id": blob.key_id,
            "ai_chat_topic_id": 11,
            "log_all_matches": True,
        }
        return NotifySettings(**{**defaults, **fields})

    def _bot(self) -> NotifierBot:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("топики уже есть — Bot API не нужен")

        return make_bot(handler)

    def _session(self, settings_row: NotifySettings, rule: Rule | None = None) -> Any:
        rows: dict[tuple[Any, Any], Any] = {(NotifySettings, SINGLETON_ID): settings_row}
        if rule is not None:
            rows[(Rule, RULE_ID)] = rule
        return FakeSession(rows)

    async def test_rule_with_its_own_topic(self) -> None:
        rule = Rule(id=RULE_ID, name="Поиск", notify_topic_enabled=True, notify_topic_id=77)
        target = await self._bot().log_target(
            self._session(self._settings_row(), rule), rule_id=RULE_ID
        )
        assert target is not None
        assert (target.group_id, target.thread_id) == (GROUP, 77)
        assert "123:abc" not in repr(target), "токен не светится в логах"

    async def test_other_rules_go_to_the_common_stream(self) -> None:
        rule = Rule(id=RULE_ID, name="Поиск", notify_topic_enabled=False, notify_topic_id=77)
        target = await self._bot().log_target(
            self._session(self._settings_row(), rule), rule_id=RULE_ID
        )
        assert target is not None
        assert target.thread_id == 11

    @pytest.mark.parametrize(
        "fields", [{"log_all_matches": False}, {"enabled": False}, {"group_id": None}]
    )
    async def test_turned_off(self, fields: dict[str, Any]) -> None:
        target = await self._bot().log_target(
            self._session(self._settings_row(**fields)), rule_id=RULE_ID
        )
        assert target is None


# --- настройка: колонка, миграция, API ------------------------------------------------
BACKEND_ROOT = Path(__file__).resolve().parents[2]


class TestLogAllMatchesSetting:
    def test_column_defaults_to_on(self) -> None:
        column = NotifySettings.__table__.c["log_all_matches"]
        assert column.nullable is False
        assert column.server_default is not None, "существующая строка получит «вкл»"
        assert column.default is not None and column.default.arg is True

    def test_migration_chains_on_xx22_without_forking_heads(self) -> None:
        config = Config()
        config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
        scripts = ScriptDirectory.from_config(config)

        assert scripts.get_heads() == ["yy23matches"]
        revision = scripts.get_revision("yy23matches")
        assert revision is not None
        assert revision.down_revision == "xx22delay"
        source = Path(revision.path).read_text(encoding="utf-8")
        assert "server_default=sa.true()" in source
        assert 'drop_column("notify_settings", "log_all_matches")' in source

    def test_api_exposes_and_accepts_the_flag(self) -> None:
        row = NotifySettings(id=SINGLETON_ID, enabled=True)
        assert notify_api._status(row).log_all_matches is True, "новая строка — «вкл»"
        row.log_all_matches = False
        assert notify_api._status(row).log_all_matches is False
        assert notify_api.NotifyUpdate(log_all_matches=False).log_all_matches is False
        assert notify_api.NotifyUpdate().log_all_matches is None, "не трогаем, если не прислали"
