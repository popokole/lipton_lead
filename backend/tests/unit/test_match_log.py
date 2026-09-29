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
from app.models.review import PendingReview
from app.notifications.match_log import (
    MAX_INCOMING_CHARS,
    MAX_REASON_CHARS,
    MAX_REQUEUES,
    PENDING_LINE,
    TELEGRAM_TEXT_LIMIT,
    CardState,
    MatchCard,
    MatchLogReporter,
    clip,
    crash_line,
    failed_line,
    format_duration,
    format_match_card,
    humanize_reason,
    outcome_line,
    skipped_line,
    unknown_line,
)
from app.notifications.notifier import (
    ChatRateLimiter,
    LogTarget,
    NotifierBot,
    NotifyError,
    NotifyRateLimitError,
    NotifyTransientError,
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
PREFIXES = ("✅", "❌", "⏭", "🟡", "⏳", "⚠️", "🎯")
#: Машинные значения, которых оператор в карточке видеть не должен.
RAW_VALUES = ("FAILED", "REJECTED", "PENDING", "CANCELLED", "SENDING", "duplicate")
NOW = datetime(2026, 9, 29, 9, 5, 0, tzinfo=UTC)


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


def analysis(
    confidence: float,
    *,
    relevant: bool = True,
    reason: str = "",
    needs_human: bool = False,
    threshold: float = 0.6,
    failed: bool = False,
    failure_reason: str | None = None,
) -> Any:
    return SimpleNamespace(
        failed=failed,
        failure_reason=failure_reason,
        threshold=threshold,
        result=SimpleNamespace(
            confidence=confidence, relevant=relevant, reason=reason, needs_human=needs_human
        ),
    )


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

    def test_other_matched_rules_are_listed(self) -> None:
        text = card_text(also_matched=["Дизайн <b>", "Логотипы"])
        header, also = text.split("\n")[:2]
        assert header.startswith("🔔 <b>Совпадение</b> · Поиск клиентов")
        assert also == "➕ Также совпали: Дизайн &lt;b&gt;, Логотипы"
        assert "Также совпали" not in card_text(), "одно правило — без строки"

    def test_worst_case_card_fits_the_telegram_limit(self) -> None:
        huge = "😀" * 5000  # эмодзи — две единицы UTF-16 каждый
        message = make_message(
            text=huge, chat_title=huge, sender_display_name=huge, sender_username=None
        )
        result = outcome_line(
            outcome(
                ActionType.REPLY,
                ActionStatus.SENT,
                reason="duplicate",
                reply_text=huge,
                dm_text=huge,
                dm_error=huge,
                analysis=analysis(0.9),
                delay_seconds=3600,
                lead_score=100,
                lead_status=huge,
            )
        )
        text = card_text(
            message,
            rule_name=huge,
            account_label=huge,
            result=result,
            also_matched=[huge] * 30,
        )
        assert utf16(visible(text)) < TELEGRAM_TEXT_LIMIT

    def test_worst_case_skip_with_ai_explanation_fits(self) -> None:
        huge = "😀" * 5000
        result = outcome_line(
            outcome(
                ActionType.ESCALATE_TO_HUMAN,
                ActionStatus.FAILED,
                reason="низкая уверенность AI (0.45 < 0.60)",
                analysis=analysis(0.45, reason=huge),
            )
        )
        text = card_text(make_message(text=huge), rule_name=huge, result=result)
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
        line = outcome_line(outcome(action, status, reason="сбой <x> & y"), now=NOW)
        assert line.startswith(PREFIXES)
        assert "<x>" not in line, "причина экранируется"

    @pytest.mark.parametrize("action", list(ActionType))
    @pytest.mark.parametrize("status", list(ActionStatus))
    def test_no_raw_machine_values_without_a_reason(
        self, action: ActionType, status: ActionStatus
    ) -> None:
        line = outcome_line(outcome(action, status), now=NOW)
        assert line.startswith(PREFIXES)
        assert not any(raw in line for raw in RAW_VALUES), line

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

    def test_sent_reply_shows_the_lead_status_and_score(self) -> None:
        line = outcome_line(
            outcome(
                ActionType.REPLY,
                ActionStatus.SENT,
                reply_text="Здравствуйте!",
                lead_score=87,
                lead_status="HOT",
            )
        )
        assert line.split("\n")[:2] == ["✅ Ответ отправлен", "🎯 Лид · HOT (87)"]

    def test_sent_reply_in_chat_and_dm(self) -> None:
        line = outcome_line(
            outcome(
                ActionType.REPLY,
                ActionStatus.SENT,
                reply_text="Отправлю в лс",
                dm_text="Развёрнутый ответ",
            )
        )
        assert line.startswith("✅ Ответ отправлен")
        assert "<b>В чат:</b>\nОтправлю в лс" in line
        assert "<b>В личку:</b>\nРазвёрнутый ответ" in line

    def test_failed_dm_is_not_a_success_and_keeps_the_text(self) -> None:
        line = outcome_line(
            outcome(
                ActionType.REPLY,
                ActionStatus.SENT,
                reply_text="Отправлю в лс",
                dm_text="Развёрнутый <ответ>",
                dm_error="PrivacyRestricted",
            )
        )
        assert line.startswith("⚠️ В чат ушло, личка НЕ ушла: PrivacyRestricted")
        assert "✅" not in line
        assert "<b>В чат:</b>\nОтправлю в лс" in line
        # Текст лички — чтобы оператор мог отправить его вручную.
        assert "<b>В личку (не доставлено):</b>\nРазвёрнутый &lt;ответ&gt;" in line

    def test_delayed_and_duplicate_marks(self) -> None:
        line = outcome_line(
            outcome(ActionType.REPLY, ActionStatus.SENT, reason="duplicate", delay_seconds=30.4)
        )
        assert "(после паузы 30 с, повтор: этот ответ уже уходил раньше)" in line
        assert "duplicate" not in line

    def test_scheduled_reply_shows_the_duration_and_local_time(self) -> None:
        line = outcome_line(
            outcome(ActionType.REPLY, ActionStatus.PENDING, delay_seconds=2847),
            tz_offset_hours=3,
            now=NOW,
        )
        # 09:05 UTC + 47 мин 27 с + 3 часа = 12:52 по местному.
        assert line == "⏳ Ответ запланирован через 47 мин 27 с (≈ в 12:52)"

    @pytest.mark.parametrize(
        ("seconds", "text"),
        [
            (0, "0 с"),
            (30.4, "30 с"),
            (59.6, "1 мин"),
            (125, "2 мин 5 с"),
            (2847, "47 мин 27 с"),
            (3600, "1 ч"),
            (3725, "1 ч 2 мин 5 с"),
            (None, "? с"),
        ],
    )
    def test_durations_are_human_readable(self, seconds: float | None, text: str) -> None:
        assert format_duration(seconds) == text

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

    def test_send_failure_whose_hand_off_failed_too(self) -> None:
        line = outcome_line(
            outcome(
                ActionType.ESCALATE_TO_HUMAN,
                ActionStatus.FAILED,
                reason="не удалось отправить ответ: fake",
                send_error="fake",
            )
        )
        assert line == "❌ Не удалось отправить: fake · ⚠️ передать оператору не удалось (сбой)"
        assert "передано оператору" not in line

    def test_interrupted_send_is_unknown_not_failed(self) -> None:
        reason = "отложенный ответ прерван во время отправки (остановка воркера) — проверьте диалог"
        line = outcome_line(
            outcome(
                ActionType.ESCALATE_TO_HUMAN,
                ActionStatus.SENT,
                reason=reason,
                send_error=reason,
                send_unknown=True,
            )
        )
        assert line == f"⚠️ Неизвестно, ушёл ли ответ: {reason} · передано оператору"
        assert "❌" not in line

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
            ("duplicate", "уже выполнялось раньше"),
            ("FAILED", "сбой"),
        ],
    )
    def test_ignore_reasons_are_human_readable(self, reason: str, expected: str) -> None:
        assert humanize_reason(reason) == expected
        line = outcome_line(outcome(ActionType.IGNORE, ActionStatus.SENT, reason=reason))
        assert line == f"⏭ Не отправляли: {html.escape(expected, quote=False)}"

    def test_ai_says_not_a_lead_with_high_confidence(self) -> None:
        """Уверенное «нет» — не «уверенность выше порога, но не ответили»."""
        line = outcome_line(
            outcome(
                ActionType.IGNORE,
                ActionStatus.SENT,
                reason="AI: не отвечать (confidence 0.95, порог 0.60)",
                analysis=analysis(0.95, relevant=False, reason="Человек сам <предлагает> услуги"),
            )
        )
        assert line == (
            "⏭ Не отправляли: ИИ — не лид (уверенность 0.95)\n"
            "ИИ: Человек сам &lt;предлагает&gt; услуги"
        )

    def test_ai_is_not_sure_enough(self) -> None:
        line = outcome_line(
            outcome(
                ActionType.IGNORE,
                ActionStatus.SENT,
                reason="AI: не отвечать (confidence 0.45, порог 0.60)",
                analysis=analysis(0.45, relevant=True),
            )
        )
        assert line == "⏭ Не отправляли: ИИ не уверен: 0.45 &lt; порога 0.60"

    def test_ai_explanation_is_clipped(self) -> None:
        line = outcome_line(
            outcome(
                ActionType.IGNORE,
                ActionStatus.SENT,
                reason="AI: не отвечать (confidence 0.95, порог 0.60)",
                analysis=analysis(0.95, relevant=False, reason="я" * 5000),
            )
        )
        note = line.split("\nИИ: ", 1)[1]
        assert utf16(note) == MAX_REASON_CHARS and note.endswith("…")

    def test_other_skip_reasons_do_not_blame_the_ai(self) -> None:
        line = outcome_line(
            outcome(
                ActionType.IGNORE,
                ActionStatus.SENT,
                reason="cooldown: user",
                analysis=analysis(0.9, reason="похоже на лида"),
            )
        )
        assert line == "⏭ Не отправляли: кулдаун — этому человеку недавно уже отвечали"

    def test_ai_failure_is_a_breakdown_not_a_decision(self) -> None:
        line = outcome_line(
            outcome(
                ActionType.ESCALATE_TO_HUMAN,
                ActionStatus.SENT,
                reason="AIError: 502 Bad Gateway",
                analysis=analysis(0.0, failed=True, failure_reason="AIError: 502 Bad Gateway"),
            )
        )
        assert line == "⏭ Не отправляли: сбой ИИ — AIError: 502 Bad Gateway · передано оператору"

    def test_low_confidence_escalation(self) -> None:
        line = outcome_line(
            outcome(
                ActionType.ESCALATE_TO_HUMAN,
                ActionStatus.SENT,
                reason="низкая уверенность AI (0.45 < 0.60)",
                analysis=analysis(0.45, reason="неясно, ищет ли услугу"),
            )
        )
        assert line == (
            "⏭ Не отправляли: ИИ не уверен: 0.45 &lt; порога 0.60 · передано оператору\n"
            "ИИ: неясно, ищет ли услугу"
        )

    def test_model_asks_for_a_human(self) -> None:
        line = outcome_line(
            outcome(
                ActionType.ESCALATE_TO_HUMAN,
                ActionStatus.SENT,
                reason="жалоба на сервис",
                analysis=analysis(0.9, needs_human=True, reason="жалоба на сервис"),
            )
        )
        assert line == (
            "⏭ Не отправляли: ИИ просит передать диалог человеку · передано оператору\n"
            "ИИ: жалоба на сервис"
        )

    def test_ignore_whose_journal_write_failed(self) -> None:
        line = outcome_line(
            outcome(ActionType.IGNORE, ActionStatus.FAILED, reason="вне рабочих часов")
        )
        assert line == "⏭ Не отправляли: вне рабочих часов (⚠️ запись в журнал не удалась: сбой)"

    @pytest.mark.parametrize(
        "reason",
        [
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
        assert line == "⏭ Не отправляли: нужен оператор · ⚠️ передать оператору не удалось (сбой)"

    @pytest.mark.parametrize(
        ("action", "label"),
        [
            (ActionType.NOTIFY_ADMIN, "уведомление в панель"),
            (ActionType.TAG_USER, "метка собеседнику"),
        ],
    )
    def test_rules_without_a_reply(self, action: ActionType, label: str) -> None:
        done = outcome_line(outcome(action, ActionStatus.SENT, reason="city=Москва"))
        assert done == f"⏭ Не отправляли: правило без ответа — {label}: выполнено (city=Москва)"
        again = outcome_line(outcome(action, ActionStatus.SENT, reason="duplicate"))
        assert again.endswith("выполнено (уже выполнялось раньше)")
        failed = outcome_line(outcome(action, ActionStatus.REJECTED, reason="нужны key и value"))
        assert failed == f"❌ Не удалось: {label} — нужны key и value"

    def test_saved_lead_shows_its_status_and_score(self) -> None:
        line = outcome_line(
            outcome(
                ActionType.SAVE_LEAD,
                ActionStatus.SENT,
                reason="WARM (55)",
                lead_score=55,
                lead_status="WARM",
            )
        )
        assert line == "🎯 Лид сохранён · WARM (55) · правило без ответа"
        again = outcome_line(outcome(ActionType.SAVE_LEAD, ActionStatus.SENT, reason="duplicate"))
        assert again == "🎯 Лид сохранён (уже выполнялось раньше) · правило без ответа"
        failed = outcome_line(
            outcome(ActionType.SAVE_LEAD, ActionStatus.REJECTED, reason="неизвестен автор")
        )
        assert failed == "❌ Не удалось: сохранить лида — неизвестен автор"

    def test_crash_line_is_escaped(self) -> None:
        line = crash_line(RuntimeError("<boom>"))
        assert line == "❌ Не удалось отправить: обработка упала — RuntimeError: &lt;boom&gt;"

    @pytest.mark.parametrize("make_line", [skipped_line, failed_line, unknown_line])
    def test_fallback_lines_are_clipped_and_escaped(self, make_line: Any) -> None:
        reason = "отложенный ответ не перепроверен: OperationalError: <SELECT " + "x" * 10_000
        line = make_line(reason)
        assert line.startswith(("⏭", "❌", "⚠️"))
        assert "<SELECT" not in line and "&lt;SELECT" in line
        assert utf16(visible(line)) < MAX_REASON_CHARS + 40


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

    async def test_other_bot_messages_take_their_share(self) -> None:
        """Ревью/лиды/дайджест идут без ожидания, но карточки им уступают."""
        clock = FakeClock()
        limiter = self._limiter(clock, max_per_window=3, window_seconds=60, min_interval_seconds=0)
        for _ in range(3):
            limiter.note(GROUP)
        assert limiter.delay(GROUP) == 60
        await limiter.acquire(GROUP)
        assert clock.sleeps == [60.0]


# --- очередь и отправка --------------------------------------------------------------
class FakeNotifier:
    """Bot API в памяти: записывает вызовы, умеет падать и тормозить."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        # Топик каждого sendMessage (в том же порядке, что calls «send»).
        self.threads: list[int | None] = []
        self.enabled = True
        self.all_matches = True
        self.stream_thread_id: int | None = None
        self.fail_send: list[Exception] = []
        self.fail_edit: list[Exception] = []
        self.gate: asyncio.Event | None = None
        self.errors: list[str] = []
        self._next_id = 100

    async def log_target(self, _db: Any, *, rule_id: uuid.UUID | None) -> LogTarget | None:
        self.calls.append(("target", rule_id))
        if not self.enabled:
            return None
        return LogTarget(
            token="t",
            group_id=GROUP,
            thread_id=THREAD,
            all_matches=self.all_matches,
            stream_thread_id=self.stream_thread_id,
        )

    async def send_log_card(
        self, target: LogTarget, text: str, *, reply_to: int | None = None
    ) -> int:
        if self.gate is not None:
            await self.gate.wait()
        self.calls.append(("send", text, reply_to))
        self.threads.append(target.thread_id)
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
    def __init__(self) -> None:
        self.executed: list[Any] = []

    @contextlib.asynccontextmanager
    async def session(self) -> AsyncIterator[Any]:
        yield self

    async def get(self, _model: Any, _ident: Any) -> Any:
        return SimpleNamespace(label="Продажи-1", username=None)

    async def execute(self, statement: Any) -> None:
        self.executed.append(statement)


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
    notifier: FakeNotifier, *, queue_limit: int = 200, database: FakeDatabase | None = None
) -> tuple[MatchLogReporter, FakeClock]:
    clock = FakeClock()
    reporter = MatchLogReporter(
        database or FakeDatabase(),  # type: ignore[arg-type]
        notifier,  # type: ignore[arg-type]
        tz_offset_hours=3,
        queue_limit=queue_limit,
        limiter=ChatRateLimiter(clock=clock, sleep=clock.sleep),
    )
    _REPORTERS.append(reporter)
    return reporter, clock


async def idle(reporter: MatchLogReporter) -> None:
    """Ждёт, пока фоновая задача разберёт очередь и уснёт."""
    for _ in range(2000):
        await asyncio.sleep(0)
        if not reporter.pending and not reporter._wakeup.is_set():
            return
    raise AssertionError("очередь не разобрана")


def open_card(reporter: MatchLogReporter, text: str = "нужен дизайнер") -> MatchCard:
    card = reporter.open_card(make_message(text), make_match())
    assert card is not None
    return card


def rate_limited(seconds: float = 3) -> NotifyRateLimitError:
    return NotifyRateLimitError("Too Many Requests", retry_after=seconds)


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
        assert "⏳ Ответ запланирован через 30 с (≈ в " in notifier.of("edit")[-1][2]

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

    async def test_other_matched_rules_are_on_the_card(self) -> None:
        notifier = FakeNotifier()
        reporter, _ = make_reporter(notifier)
        card = reporter.open_card(make_message(), make_match(), also_matched=["Логотипы"])
        assert card is not None
        await idle(reporter)
        assert "➕ Также совпали: Логотипы" in notifier.of("send")[0][1]

    # --- 429 и временные сбои -------------------------------------------------
    async def test_429_waits_retry_after_and_retries_once(self) -> None:
        notifier = FakeNotifier()
        notifier.fail_send = [rate_limited(7)]
        reporter, clock = make_reporter(notifier)

        card = open_card(reporter)
        card.report(SENT)
        await idle(reporter)

        assert len(notifier.of("send")) == 2, "одна повторная попытка"
        assert 7.0 in clock.sleeps, "ждали столько, сколько сказал Telegram"
        assert card.state is CardState.POSTED
        assert reporter.throttled == 1

    async def test_429_twice_on_a_card_with_its_result_requeues_it(self) -> None:
        """Итог уже внутри карточки — второй 429 не должен её потерять."""
        notifier = FakeNotifier()
        notifier.fail_send = [rate_limited(), rate_limited()]
        reporter, _ = make_reporter(notifier)

        card = open_card(reporter)
        card.report(SENT)
        await idle(reporter)

        sends = notifier.of("send")
        assert len(sends) == 3, "две попытки, потом — в конец очереди, и ушла"
        assert "✅ Ответ отправлен" in sends[-1][1]
        assert card.state is CardState.POSTED
        assert reporter.requeued == 1

    async def test_429_twice_on_an_edit_requeues_the_final_result(self) -> None:
        notifier = FakeNotifier()
        reporter, _ = make_reporter(notifier)
        card = open_card(reporter)
        await idle(reporter)

        notifier.fail_edit = [rate_limited(), rate_limited()]
        card.report(SENT)
        await idle(reporter)

        assert len(notifier.of("edit")) == 3
        assert card.shown_result == card.result, "итог всё-таки в карточке"
        assert not card.edit_broken
        assert reporter.requeued == 1

    async def test_persistent_429_gives_up_loudly(self) -> None:
        notifier = FakeNotifier()
        reporter, _ = make_reporter(notifier)
        card = open_card(reporter)
        await idle(reporter)

        notifier.fail_edit = [rate_limited() for _ in range(2 * (MAX_REQUEUES + 1))]
        card.report(SENT)
        await idle(reporter)

        assert len(notifier.of("edit")) == 2 * (MAX_REQUEUES + 1), "ограниченное число попыток"
        assert reporter.requeued == MAX_REQUEUES
        assert reporter.failed == 1
        assert notifier.errors, "ошибка видна в настройках бота"
        assert card.shown_result != card.result

    async def test_network_error_on_edit_is_retried_not_turned_into_replies(self) -> None:
        notifier = FakeNotifier()
        reporter, _ = make_reporter(notifier)
        card = open_card(reporter)
        await idle(reporter)

        notifier.fail_edit = [httpx.ConnectError("boom"), NotifyTransientError("Bad Gateway")]
        card.report(SENT)
        await idle(reporter)

        assert len(notifier.of("edit")) == 3
        assert len(notifier.of("send")) == 1, "итог не ушёл отдельным ответом"
        assert not card.edit_broken
        assert card.shown_result == card.result

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
        assert reply[1].startswith("⏳ Ответ запланирован через 30 с")
        assert reply[2] == card.message_id
        assert notifier.threads[-1] == THREAD, "ответ — в том же топике"
        assert card.edit_broken

        # Дальше итог сразу ответом, без заведомо неудачной правки.
        card.report(SENT)
        await idle(reporter)
        assert len(notifier.of("edit")) == 1
        assert notifier.of("send")[-1][2] == card.message_id
        assert notifier.of("send")[-1][1].startswith("✅ Ответ отправлен")

    async def test_other_edit_errors_reply_once_but_keep_trying_edits(self) -> None:
        notifier = FakeNotifier()
        reporter, _ = make_reporter(notifier)
        card = open_card(reporter)
        await idle(reporter)

        notifier.fail_edit = [NotifyError("Bad Request: can't parse entities")]
        card.report(SCHEDULED)
        await idle(reporter)
        assert notifier.of("send")[-1][2] == card.message_id
        assert not card.edit_broken

        card.report(SENT)
        await idle(reporter)
        assert len(notifier.of("edit")) == 2, "следующий итог — снова правкой"

    async def test_permanent_send_error_waits_for_the_next_result(self) -> None:
        notifier = FakeNotifier()
        notifier.fail_send = [NotifyError("Bad Request: chat not found")]
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
        assert len(sends) == 2
        assert "✅ Ответ отправлен" in sends[-1][1]
        assert card.state is CardState.POSTED

    # --- выключатели ----------------------------------------------------------
    async def test_nothing_is_sent_when_notifications_are_off(self) -> None:
        notifier = FakeNotifier()
        notifier.enabled = False
        reporter, _ = make_reporter(notifier)

        card = open_card(reporter)
        await idle(reporter)
        card.report(SENT)
        await idle(reporter)

        assert card.state is CardState.SKIPPED
        assert notifier.of("send") == [] and notifier.of("edit") == []

    async def test_every_match_mode_off_still_reports_sent_replies(self) -> None:
        """Как было до режима: карточка об отправленном ответе приходит."""
        notifier = FakeNotifier()
        notifier.all_matches = False
        reporter, _ = make_reporter(notifier)

        card = open_card(reporter)
        await idle(reporter)
        assert card.state is CardState.HELD
        assert notifier.of("send") == [], "совпадение без итога не шлём"

        card.report(SENT)
        await idle(reporter)

        (send,) = notifier.of("send")
        assert "✅ Ответ отправлен" in send[1] and "нужен дизайнер" in send[1]
        assert notifier.of("edit") == []
        assert card.state is CardState.POSTED

    @pytest.mark.parametrize(
        "result",
        [
            IGNORED,
            outcome(ActionType.REQUEST_REVIEW, ActionStatus.SENT),
            outcome(ActionType.ESCALATE_TO_HUMAN, ActionStatus.SENT, reason="нужен оператор"),
        ],
    )
    async def test_every_match_mode_off_skips_matches_without_a_reply(
        self, result: ReplyOutcome
    ) -> None:
        notifier = FakeNotifier()
        notifier.all_matches = False
        reporter, _ = make_reporter(notifier)

        card = open_card(reporter)
        await idle(reporter)
        card.report(result)
        await idle(reporter)

        assert notifier.of("send") == [] and notifier.of("edit") == []
        assert card.state is CardState.SKIPPED

    async def test_every_match_mode_off_with_a_delayed_reply(self) -> None:
        notifier = FakeNotifier()
        notifier.all_matches = False
        reporter, _ = make_reporter(notifier)

        card = open_card(reporter)
        card.report(SCHEDULED)
        await idle(reporter)
        assert notifier.of("send") == [], "«⏳» — ещё не ответ"

        card.report(SENT)
        await idle(reporter)
        assert len(notifier.of("send")) == 1

    async def test_every_match_mode_off_result_known_before_posting(self) -> None:
        notifier = FakeNotifier()
        notifier.all_matches = False
        reporter, _ = make_reporter(notifier)

        open_card(reporter).report(SENT)
        open_card(reporter, "второе").report(IGNORED)
        await idle(reporter)

        (send,) = notifier.of("send")
        assert "✅ Ответ отправлен" in send[1] and "нужен дизайнер" in send[1]

    # --- общий поток и ревью --------------------------------------------------
    async def test_rule_topic_reply_is_noted_in_the_common_stream(self) -> None:
        notifier = FakeNotifier()
        notifier.stream_thread_id = 11
        reporter, _ = make_reporter(notifier)

        card = open_card(reporter)
        await idle(reporter)
        card.report(SENT)
        await idle(reporter)
        card.report(SENT)  # повтор итога — второй строки нет
        await idle(reporter)

        sends = notifier.of("send")
        assert len(sends) == 2
        assert notifier.threads == [THREAD, 11]
        note = sends[-1][1]
        assert note.startswith("✅ Ответ отправлен · <b>Поиск клиентов</b> · 👤 Иван")
        assert f'<a href="https://t.me/c/777/{card.message_id}">карточка</a>' in note

    async def test_failed_dm_is_flagged_in_the_stream_note(self) -> None:
        notifier = FakeNotifier()
        notifier.stream_thread_id = 11
        reporter, _ = make_reporter(notifier)

        open_card(reporter).report(
            outcome(
                ActionType.REPLY,
                ActionStatus.SENT,
                reply_text="Отправлю в лс",
                dm_text="Ответ",
                dm_error="PrivacyRestricted",
            )
        )
        await idle(reporter)
        assert notifier.of("send")[-1][1].startswith("⚠️ В чат ушло, личка НЕ ушла · ")

    @pytest.mark.parametrize("result", [IGNORED, SCHEDULED])
    async def test_no_stream_note_without_a_sent_reply(self, result: ReplyOutcome) -> None:
        notifier = FakeNotifier()
        notifier.stream_thread_id = 11
        reporter, _ = make_reporter(notifier)

        open_card(reporter).report(result)
        await idle(reporter)

        assert notifier.threads == [THREAD]

    async def test_no_stream_note_for_cards_already_in_the_stream(self) -> None:
        notifier = FakeNotifier()  # stream_thread_id=None: карточка и так в потоке
        reporter, _ = make_reporter(notifier)

        open_card(reporter).report(SENT)
        await idle(reporter)

        assert len(notifier.of("send")) == 1

    async def test_review_card_is_linked_for_the_operator_decision(self) -> None:
        notifier = FakeNotifier()
        database = FakeDatabase()
        reporter, _ = make_reporter(notifier, database=database)
        review_id = uuid.uuid4()

        card = open_card(reporter)
        await idle(reporter)
        card.report(outcome(ActionType.REQUEST_REVIEW, ActionStatus.SENT, review_id=review_id))
        await idle(reporter)

        (statement,) = database.executed
        assert statement.table.name == PendingReview.__tablename__
        params = statement.compile().params
        assert params["match_card_message_id"] == card.message_id
        assert params["match_card_thread_id"] == THREAD
        assert review_id in params.values()
        assert card.review_linked

    # --- очередь --------------------------------------------------------------
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

    async def test_overflow_drops_stream_notes_before_cards(self) -> None:
        notifier = FakeNotifier()
        notifier.gate = asyncio.Event()  # бот висит — всё копится в очереди
        reporter, _ = make_reporter(notifier, queue_limit=3)
        first = open_card(reporter)
        for _ in range(10):
            await asyncio.sleep(0)
        assert first.state is CardState.POSTING

        reporter._enqueue(_mirror_op(first))
        open_card(reporter, "m1")
        open_card(reporter, "m2")
        open_card(reporter, "m3")  # переполнение

        assert reporter.dropped_notes == 1
        assert reporter.dropped_cards == 0
        assert [op.kind for op in reporter._ops] == ["post"] * 3

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
        assert reporter.dropped_cards == 1, "карточка после остановки — не молча"

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


def _mirror_op(card: MatchCard) -> Any:
    from app.notifications.match_log import _Op

    return _Op("mirror", card)


# --- NotifierBot: Bot API ---------------------------------------------------------
def make_bot(handler: Any, **kwargs: Any) -> NotifierBot:
    bot = NotifierBot(build_secret_box(make_settings()), **kwargs)
    bot._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return bot


TARGET = LogTarget(token="123:abc", group_id=GROUP, thread_id=THREAD)


def too_many_requests(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        429,
        json={
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests: retry after 7",
            "parameters": {"retry_after": 7},
        },
    )


class TestNotifierBotLogCards:
    async def test_429_raises_with_retry_after(self) -> None:
        bot = make_bot(too_many_requests)
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
        assert not isinstance(error.value, NotifyRateLimitError | NotifyTransientError)

    @pytest.mark.parametrize(
        "response",
        [
            httpx.Response(502, text="<html>Bad Gateway</html>"),
            httpx.Response(
                500, json={"ok": False, "error_code": 500, "description": "Internal Server Error"}
            ),
        ],
    )
    async def test_server_errors_are_transient(self, response: httpx.Response) -> None:
        bot = make_bot(lambda _request: response)
        with pytest.raises(NotifyTransientError):
            await bot.send_log_card(TARGET, "текст")

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
        assert "counted" not in seen[0], "служебный флаг не уходит в Bot API"
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

    async def test_every_group_message_counts_in_the_shared_limit(self) -> None:
        """Ревью/лиды/дайджест отмечаются в лимите — карточки им уступают."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 9}})

        clock = FakeClock()
        limiter = ChatRateLimiter(
            max_per_window=2, window_seconds=60, min_interval_seconds=0, clock=clock
        )
        bot = make_bot(handler, limiter=limiter)
        await bot._call("t", "sendMessage", chat_id=GROUP, text="ревью")
        await bot._call("t", "editMessageReplyMarkup", chat_id=GROUP, message_id=1)
        await bot._call("t", "getChat", chat_id=GROUP)  # не пишет в чат — не считается
        assert limiter.delay(GROUP) == 60

        # Карточка совпадения место уже заняла (acquire) — второй раз не считаем.
        await bot.send_log_card(LogTarget(token="t", group_id=GROUP + 1, thread_id=None), "x")
        assert limiter.delay(GROUP + 1) == 0

    async def test_429_blocks_the_shared_limit(self) -> None:
        clock = FakeClock()
        limiter = ChatRateLimiter(clock=clock, min_interval_seconds=0)
        bot = make_bot(too_many_requests, limiter=limiter)

        with pytest.raises(NotifyRateLimitError):
            await bot._call("t", "sendMessage", chat_id=GROUP, text="лид")
        assert limiter.delay(GROUP) == 7


class TestSendReviewRetry:
    def _review(self) -> Any:
        return SimpleNamespace(
            id=uuid.uuid4(),
            chat_id=None,
            sender_username=None,
            sender_display_name="Иван",
            target_sender_tg_id=4242,
            confidence=0.5,
            tg_chat_id=-100_123,
            reply_to_tg_message_id=7,
            incoming_text="нужен дизайнер",
            dm_text=None,
            reply_text="Здравствуйте!",
            notify_message_id=None,
        )

    async def _send(self, responses: list[httpx.Response]) -> tuple[Any, list[float]]:
        slept: list[float] = []

        async def sleep(seconds: float) -> None:
            slept.append(seconds)

        def handler(_request: httpx.Request) -> httpx.Response:
            return responses.pop(0)

        bot = make_bot(handler, sleep=sleep)
        bot._load_settings = _async_value(("t", GROUP))  # type: ignore[method-assign]
        bot._ensure_stream_topic = _async_value(11)  # type: ignore[method-assign]
        review = self._review()
        await bot.send_review(FakeSession({}), review)
        return review, slept

    async def test_retries_once_after_retry_after(self) -> None:
        review, slept = await self._send(
            [
                too_many_requests(httpx.Request("POST", "https://x")),
                httpx.Response(200, json={"ok": True, "result": {"message_id": 55}}),
            ]
        )
        assert slept == [7.0]
        assert review.notify_message_id == 55

    async def test_long_retry_after_is_not_waited_inline(self) -> None:
        long_wait = httpx.Response(
            429,
            json={
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests",
                "parameters": {"retry_after": 120},
            },
        )
        review, slept = await self._send([long_wait])
        assert slept == []
        assert review.notify_message_id is None


def _async_value(value: Any) -> Any:
    async def call(*_args: Any, **_kwargs: Any) -> Any:
        return value

    return call


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
        assert target.stream_thread_id == 11, "ответы — ещё строкой в общий поток"
        assert target.all_matches
        assert "123:abc" not in repr(target), "токен не светится в логах"

    async def test_other_rules_go_to_the_common_stream(self) -> None:
        rule = Rule(id=RULE_ID, name="Поиск", notify_topic_enabled=False, notify_topic_id=77)
        target = await self._bot().log_target(
            self._session(self._settings_row(), rule), rule_id=RULE_ID
        )
        assert target is not None
        assert target.thread_id == 11
        assert target.stream_thread_id is None, "карточка и так в общем потоке"

    async def test_every_match_mode_off_keeps_the_target(self) -> None:
        target = await self._bot().log_target(
            self._session(self._settings_row(log_all_matches=False)), rule_id=RULE_ID
        )
        assert target is not None, "об отправленных ответах карточки идут и так"
        assert target.all_matches is False

    @pytest.mark.parametrize("fields", [{"enabled": False}, {"group_id": None}])
    async def test_turned_off(self, fields: dict[str, Any]) -> None:
        target = await self._bot().log_target(
            self._session(self._settings_row(**fields)), rule_id=RULE_ID
        )
        assert target is None

    @pytest.mark.parametrize(("value", "expected"), [(True, True), (False, False)])
    async def test_every_match_flag_for_save_lead(self, value: bool, expected: bool) -> None:
        session = self._session(self._settings_row(log_all_matches=value))
        assert await self._bot().log_all_matches_enabled(session) is expected
        assert await self._bot().log_all_matches_enabled(FakeSession({})) is True


# --- настройка: колонка, миграция, API ------------------------------------------------
BACKEND_ROOT = Path(__file__).resolve().parents[2]


class TestLogAllMatchesSetting:
    def test_column_defaults_to_on(self) -> None:
        column = NotifySettings.__table__.c["log_all_matches"]
        assert column.nullable is False
        assert column.server_default is not None, "существующая строка получит «вкл»"
        assert column.default is not None and column.default.arg is True

    def test_review_keeps_a_link_to_the_match_card(self) -> None:
        columns = PendingReview.__table__.c
        assert columns["match_card_message_id"].nullable
        assert columns["match_card_thread_id"].nullable

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
        assert '"pending_reviews", sa.Column("match_card_message_id"' in source
        assert 'drop_column("pending_reviews", "match_card_thread_id")' in source

    def test_api_exposes_and_accepts_the_flag(self) -> None:
        row = NotifySettings(id=SINGLETON_ID, enabled=True)
        assert notify_api._status(row).log_all_matches is True, "новая строка — «вкл»"
        row.log_all_matches = False
        assert notify_api._status(row).log_all_matches is False
        assert notify_api.NotifyUpdate(log_all_matches=False).log_all_matches is False
        assert notify_api.NotifyUpdate().log_all_matches is None, "не трогаем, если не прислали"
