"""Карточки совпадений правил в лог-чате (форум-группа бота-уведомителя).

Владелец хочет видеть в лог-чате КАЖДОЕ сообщение, совпавшее с правилом, а не
только те, на которые ушёл ответ. Поэтому:

1. Сразу после совпадения (после claim() в MonitorPipeline — одно и то же
   сообщение не попадёт дважды, даже если его довыгрузит реконсайлер) ставим
   в очередь карточку: правило (и какие ещё совпали), аккаунт, чат и ссылка,
   отправитель, текст, время. Маршрут — как у notify_lead: свой топик
   правила, если включён, иначе общий поток «Общение ИИ».
2. Когда итог известен, та же карточка редактируется (editMessageText): ✅
   ответ ушёл (текст ответа и лички, лид и его балл) / ⚠️ в чат ушло, личка
   нет / ❌ не удалось отправить / ⏭ не отправляли и почему / 🟡 на проверке
   у оператора / ⏳ ответ запланирован (потом — итог) / 🎯 лид сохранён. Если
   отредактировать уже нельзя (сообщение удалено, слишком старое) — итог
   уходит ответом на карточку в тот же топик.
3. Правило со своим топиком: об отправленном ответе ещё и короткая строка со
   ссылкой на карточку в общий поток «Общение ИИ» — там по-прежнему видны все
   наши ответы.

Режим выключен (notify_settings.log_all_matches = false) — как было до него:
карточка уходит, только если ответ отправлен (CardState.HELD ждёт итога).

Всё это вторично по отношению к ответу лиду: конвейер только кладёт задание в
очередь (синхронно, без await и без исключений), а в Bot API ходит одна
фоновая задача. Она соблюдает лимит Telegram (~20 сообщений в минуту на
группу, общий ChatRateLimiter бота) и retry_after из 429: ждёт и повторяет;
временные сбои (429, 5xx, сеть) ставят задание в конец очереди ещё до
MAX_REQUEUES раз. Если очередь переполнена, выбрасываются сначала строки в
общий поток, потом самые старые ещё не отправленные карточки — с
предупреждением в журнале и счётчиком, а не молча.

Ссылка на карточку (MatchCard) идёт вместе с обработкой сообщения:
MonitorPipeline → ReplyPipeline.handle → _PreparedReply → фоновая задача
отложенного ответа, которая и дописывает итог после паузы. Хранится она только
в памяти: после жёсткого падения воркера карточка так и останется с «⏳», а
зависший ответ отдаст оператору sweep_stale_scheduled. Для ответа на проверке
id карточки пишется в PendingReview — решение оператора (кнопки, панель)
дописывается к ней ответом.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import re
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from sqlalchemy import update

from app.core.clock import utcnow
from app.core.logging import get_logger
from app.database.session import Database
from app.models import Account, ActionStatus, ActionType, PendingReview
from app.notifications.notifier import (
    ChatRateLimiter,
    LogTarget,
    NotifierBot,
    NotifyRateLimitError,
    is_transient,
    is_uneditable,
    message_link,
)
from app.rules.engine import RuleMatch
from app.telegram.messages import NormalizedMessage

if TYPE_CHECKING:
    from app.ai.analyzer import AnalysisOutcome
    from app.pipeline.reply_pipeline import ReplyOutcome

logger = get_logger(__name__)

T = TypeVar("T")

#: Лимиты длины частей карточки — в UTF-16 (так считает Telegram). Сумма
#: с запасом меньше 4096: сообщение не упадёт с «message is too long».
MAX_INCOMING_CHARS = 700
MAX_REPLY_CHARS = 700
MAX_NAME_CHARS = 100
MAX_REASON_CHARS = 400
MAX_ALSO_MATCHED_CHARS = 200
MAX_LEAD_STATUS_CHARS = 20
TELEGRAM_TEXT_LIMIT = 4096

#: Очередь заданий к Bot API. При переполнении выбрасываются сначала строки в
#: общий поток, потом самые старые ещё не отправленные карточки.
DEFAULT_QUEUE_LIMIT = 200
#: Сколько при остановке воркера ждём, пока очередь дойдёт до конца.
DEFAULT_CLOSE_GRACE_SECONDS = 5.0
#: Сколько раз задание карточки после временного сбоя (429 и после повтора,
#: 5xx, сеть) встаёт в конец очереди, прежде чем мы сдадимся (с записью в
#: журнал и в last_error бота). Лимитёр уже ждёт retry_after, так что это не
#: крутится вхолостую.
MAX_REQUEUES = 3

PENDING_LINE = "⌛ Обрабатываю…"
STOPLIST_LINE = "⏭ Не отправляли: отправитель в стоп-листе"
NO_REPLY_PIPELINE_LINE = "⏭ Не отправляли: ответы на этом воркере выключены"
#: Отмена (остановка воркера) до того, как дошло до ответа.
INTERRUPTED_BEFORE_REPLY_LINE = "⏭ Не отправляли: обработка прервана (остановка воркера)"
#: Отмена посреди обработки ответа: отправка могла уже начаться.
INTERRUPTED_LINE = (
    "⚠️ Обработка прервана (остановка воркера) — неизвестно, ушёл ли ответ; проверьте диалог"
)
SENT_HEAD = "✅ Ответ отправлен"
DM_FAILED_HEAD = "⚠️ В чат ушло, личка НЕ ушла"


# --- текст карточки ----------------------------------------------------------------
def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def clip(text: str, limit: int) -> str:
    """Обрезает текст до limit единиц UTF-16 (как считает Telegram), с «…».

    Режем по символам Python, поэтому суррогатная пара (эмодзи) никогда не
    разрывается пополам. Экранировать — ПОСЛЕ обрезки, иначе можно разрезать
    сущность «&amp;».
    """
    if _utf16_len(text) <= limit:
        return text
    budget = max(limit - 1, 0)
    out: list[str] = []
    used = 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if used + width > budget:
            break
        out.append(char)
        used += width
    return "".join(out).rstrip() + "…"


def format_duration(seconds: float | None) -> str:
    """Пауза по-человечески: «35 с», «47 мин 27 с», «1 ч»."""
    if seconds is None:
        return "? с"
    total = max(round(seconds), 0)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours} ч")
    if minutes:
        parts.append(f"{minutes} мин")
    if secs or not parts:
        parts.append(f"{secs} с")
    return " ".join(parts)


def _peer_key(chat_id: int | None) -> int | None:
    """Приводит id чата к одному виду: Bot API (-100…/-…) и «голый» id Telethon."""
    if chat_id is None:
        return None
    if chat_id <= -1_000_000_000_000:
        return -chat_id - 1_000_000_000_000
    return abs(chat_id)


def is_own_log_traffic(message: Any, target: Any) -> bool:
    """Сообщение из самого лог-чата или от нашего бота уведомлений.

    На такие сообщения карточек не бывает никогда — иначе аккаунт, который
    состоит в лог-чате, видит нашу же карточку, правило срабатывает на её
    текст, и получается бесконечная петля.
    """
    chat = _peer_key(getattr(message, "tg_chat_id", None))
    if chat is not None and chat == _peer_key(getattr(target, "group_id", None)):
        return True
    sender = getattr(message, "sender_tg_id", None)
    token = getattr(target, "token", "") or ""
    bot_id = token.split(":", 1)[0]
    return sender is not None and bot_id.isdigit() and int(bot_id) == int(sender)


def format_match_card(
    *,
    rule_name: str,
    account_label: str,
    chat_title: str | None,
    chat_username: str | None,
    tg_chat_id: int | None,
    tg_message_id: int | None,
    is_private: bool,
    sender_name: str | None,
    sender_username: str | None,
    sender_tg_id: int | None,
    incoming_text: str,
    date: datetime | None,
    tz_offset_hours: int = 0,
    result: str | None = None,
    also_matched: Sequence[str] = (),
) -> str:
    """Карточка совпадения: правило, кто, откуда, когда, что написал, итог.

    result — уже готовый HTML итога (см. outcome_line); None — итог ещё не
    известен. also_matched — остальные совпавшие правила (stop_on_match
    выключен). Всё пользовательское экранируется и обрезается.
    """
    handle = f"@{sender_username}" if sender_username else None
    who = sender_name or handle or str(sender_tg_id or "?")
    sender_line = f"👤 {_esc(clip(who, MAX_NAME_CHARS))}"
    if handle and who != handle:
        sender_line += f" · {_esc(handle)}"
    elif not handle and sender_tg_id:
        sender_line += f' · <a href="tg://user?id={int(sender_tg_id)}">написать</a>'
    if is_private:
        where = "личка"
    else:
        where = chat_title or (f"@{chat_username}" if chat_username else "группа")
    where_line = f"💬 {_esc(clip(where, MAX_NAME_CHARS))}"
    link = None if is_private else message_link(tg_chat_id, tg_message_id, chat_username)
    if link:
        where_line += f' · <a href="{html.escape(link, quote=True)}">сообщение</a>'
    where_line += f" · аккаунт {_esc(clip(account_label, MAX_NAME_CHARS))}"

    lines = [f"🔔 <b>Совпадение</b> · {_esc(clip(rule_name, MAX_NAME_CHARS))}"]
    if also_matched:
        names = clip(", ".join(also_matched), MAX_ALSO_MATCHED_CHARS)
        lines.append(f"➕ Также совпали: {_esc(names)}")
    lines += [sender_line, where_line]
    if date is not None:
        local = date + timedelta(hours=tz_offset_hours)
        lines.append(f"🕒 {local:%d.%m %H:%M:%S}")
    text = incoming_text.strip() or "(без текста)"
    lines += ["", "<b>Сообщение:</b>", _esc(clip(text, MAX_INCOMING_CHARS))]
    lines += ["", result or PENDING_LINE]
    return "\n".join(lines)


def format_stream_note(
    *, head: str, rule_name: str, message: NormalizedMessage, card_link: str | None
) -> str:
    """Строка в общий поток «Общение ИИ» об ответе по правилу со своим топиком."""
    handle = f"@{message.sender_username}" if message.sender_username else None
    who = message.sender_display_name or handle or str(message.sender_tg_id or "?")
    parts = [
        f"{head} · <b>{_esc(clip(rule_name, MAX_NAME_CHARS))}</b>",
        f"👤 {_esc(clip(who, MAX_NAME_CHARS))}",
    ]
    if card_link:
        parts.append(f'<a href="{html.escape(card_link, quote=True)}">карточка</a>')
    return " · ".join(parts)


# --- итог обработки → строка карточки -------------------------------------------
_COOLDOWN_SCOPES = {
    "user": "этому человеку недавно уже отвечали",
    "chat": "в этот чат недавно уже отвечали",
    "account": "аккаунт недавно уже отвечал",
    "rule": "правило недавно уже срабатывало",
    "scenario": "сценарий недавно уже отвечал",
}
_AI_SKIP = re.compile(r"^AI: не отвечать \(confidence ([\d.]+), порог ([\d.]+)\)$")
_LOW_CONFIDENCE_PREFIX = "низкая уверенность AI"
_EXACT_REASONS = {
    "анти-бан: лимит на чат": "анти-бан: в этот чат недавно уже отвечали",
    "вне рабочих часов": "вне рабочих часов",
    "one_shot: уже связались": "«один заход»: с этим человеком уже связывались",
    "one_shot: ответ уже запланирован": "«один заход»: ответ этому человеку уже запланирован",
    "duplicate": "уже выполнялось раньше",
    "ignored": "пропущено",
}
_SIMPLE_ACTIONS = {
    ActionType.NOTIFY_ADMIN: "уведомление в панель",
    ActionType.SAVE_LEAD: "сохранить лида",
    ActionType.TAG_USER: "метка собеседнику",
}
_STATUS_RU = {
    ActionStatus.PENDING: "в очереди",
    ActionStatus.VALIDATING: "на проверке",
    ActionStatus.READY: "готово к отправке",
    ActionStatus.SENDING: "отправляется",
    ActionStatus.SENT: "выполнено",
    ActionStatus.FAILED: "сбой",
    ActionStatus.CANCELLED: "отменено",
    ActionStatus.REJECTED: "отклонено",
}


def status_ru(status: ActionStatus) -> str:
    """Статус действия словами оператора, а не «FAILED»."""
    return _STATUS_RU.get(status, str(status.value).lower())


def humanize_reason(reason: str | None) -> str:
    """Машинная причина конвейера → понятная оператору фраза."""
    if not reason:
        return "причина не указана"
    reason = reason.strip()
    if reason.startswith("cooldown: "):
        scope = reason.removeprefix("cooldown: ")
        return f"кулдаун — {_COOLDOWN_SCOPES.get(scope, scope)}"
    if reason in _EXACT_REASONS:
        return _EXACT_REASONS[reason]
    if reason in ActionStatus.__members__:
        return status_ru(ActionStatus(reason))
    ai = _AI_SKIP.match(reason)
    if ai:
        return f"ИИ решил не отвечать (уверенность {ai.group(1)} при пороге {ai.group(2)})"
    return reason


def _reason(text: str | None) -> str:
    return _esc(clip(humanize_reason(text), MAX_REASON_CHARS))


def skipped_line(reason: str | None) -> str:
    """«⏭ Не отправляли: …» — готовый (экранированный, обрезанный) HTML."""
    return f"⏭ Не отправляли: {_reason(reason)}"


def failed_line(reason: str | None) -> str:
    """«❌ Не удалось отправить: …» — готовый HTML."""
    return f"❌ Не удалось отправить: {_reason(reason)}"


def unknown_line(reason: str | None) -> str:
    """«⚠️ Неизвестно, ушёл ли ответ: …» — отправка прервана на середине."""
    return f"⚠️ Неизвестно, ушёл ли ответ: {_reason(reason)}"


def _confidence(outcome: ReplyOutcome) -> str:
    analysis = outcome.analysis
    if analysis is None or analysis.failed:
        return ""
    return f" · уверенность ИИ {analysis.result.confidence:.2f}"


def _ai_verdict(analysis: AnalysisOutcome) -> str | None:
    """Решение ИИ «не отвечать» словами — без противоречия «уверенность 0.95 и
    не ответили»: высокая уверенность в «не лид» — это уверенное «нет»."""
    if analysis.failed:
        return f"сбой ИИ — {analysis.failure_reason or 'ИИ недоступен'}"
    result = analysis.result
    if not result.relevant:
        return f"ИИ — не лид (уверенность {result.confidence:.2f})"
    if result.confidence < analysis.threshold:
        return f"ИИ не уверен: {result.confidence:.2f} < порога {analysis.threshold:.2f}"
    return None


def _skip_reason(outcome: ReplyOutcome) -> tuple[str, bool]:
    """Причина «не отправляли» (HTML) и нужно ли пояснение модели под ней."""
    reason = outcome.reason
    analysis = outcome.analysis
    if analysis is not None:
        verdict: str | None = None
        if outcome.action is ActionType.IGNORE:
            if reason and _AI_SKIP.match(reason.strip()):
                verdict = _ai_verdict(analysis)
        elif analysis.failed:
            # Сбой анализатора — поломка, а не решение модели: так и пишем.
            return _esc(clip(_ai_verdict(analysis) or "сбой ИИ", MAX_REASON_CHARS)), False
        elif reason and reason.startswith(_LOW_CONFIDENCE_PREFIX):
            verdict = _ai_verdict(analysis)
        elif analysis.result.needs_human and reason == (
            analysis.result.reason or "модель просит передать человеку"
        ):
            verdict = "ИИ просит передать диалог человеку"
        if verdict is not None:
            return _esc(clip(verdict, MAX_REASON_CHARS)), True
    return _reason(reason), False


def _ai_note(outcome: ReplyOutcome) -> str:
    """Пояснение самой модели (analysis.result.reason) отдельной строкой."""
    analysis = outcome.analysis
    if analysis is None or analysis.failed:
        return ""
    explanation = (analysis.result.reason or "").strip()
    if not explanation:
        return ""
    return f"\nИИ: {_esc(clip(explanation, MAX_REASON_CHARS))}"


def _handover(outcome: ReplyOutcome) -> str:
    if outcome.action is not ActionType.ESCALATE_TO_HUMAN:
        return ""
    if outcome.status is ActionStatus.SENT:
        return " · передано оператору"
    return f" · ⚠️ передать оператору не удалось ({status_ru(outcome.status)})"


def _eta(delay: float | None, tz_offset_hours: int, now: datetime | None) -> str:
    if delay is None:
        return ""
    moment = (now or utcnow()) + timedelta(seconds=delay, hours=tz_offset_hours)
    return f" (≈ в {moment:%H:%M})"


def _lead_line(outcome: ReplyOutcome, prefix: str) -> str | None:
    if not outcome.lead_status:
        return None
    status = _esc(clip(outcome.lead_status, MAX_LEAD_STATUS_CHARS))
    score = f" ({outcome.lead_score})" if outcome.lead_score is not None else ""
    return f"{prefix} · {status}{score}"


def outcome_line(
    outcome: ReplyOutcome, *, tz_offset_hours: int = 0, now: datetime | None = None
) -> str:
    """Итог обработки совпадения — готовый (экранированный) HTML для карточки.

    ✅ ответ ушёл · ⚠️ в чат ушло, личка нет / неизвестно, ушёл ли ответ ·
    ❌ отправка не удалась · ⏭ не отправляли и почему · 🟡 на проверке у
    оператора · ⏳ ответ запланирован (с ожидаемым местным временем) ·
    🎯 лид сохранён.
    """
    action, status = outcome.action, outcome.status
    handover = _handover(outcome)

    if outcome.send_unknown:
        return f"{unknown_line(outcome.send_error or outcome.reason)}{handover}"
    if outcome.send_error is not None:
        return f"{failed_line(outcome.send_error)}{handover}"

    if action is ActionType.REPLY:
        if status is ActionStatus.PENDING:
            delay = outcome.delay_seconds
            return (
                f"⏳ Ответ запланирован через {format_duration(delay)}"
                f"{_eta(delay, tz_offset_hours, now)}{_confidence(outcome)}"
            )
        if status is not ActionStatus.SENT:
            return failed_line(outcome.reason or status_ru(status))
        return _sent_block(outcome)

    if action is ActionType.REQUEST_REVIEW:
        if status is ActionStatus.SENT:
            return (
                "🟡 Не отправляли: ответ на проверке у оператора "
                f"(карточка с кнопками — в «Общение ИИ»){_confidence(outcome)}"
            )
        return (
            "❌ Не удалось отдать ответ на проверку оператору: "
            f"{_reason(outcome.reason or status_ru(status))}"
        )

    if action in (ActionType.IGNORE, ActionType.ESCALATE_TO_HUMAN):
        reason, with_note = _skip_reason(outcome)
        line = f"⏭ Не отправляли: {reason}"
        if action is ActionType.IGNORE and status is not ActionStatus.SENT:
            line += f" (⚠️ запись в журнал не удалась: {status_ru(status)})"
        line += handover
        if with_note:
            line += _ai_note(outcome)
        return line

    if action is ActionType.SAVE_LEAD and status is ActionStatus.SENT:
        lead = _lead_line(outcome, "🎯 Лид сохранён")
        if lead is None:
            detail = f" ({_reason(outcome.reason)})" if outcome.reason else ""
            lead = f"🎯 Лид сохранён{detail}"
        return f"{lead} · правило без ответа"

    label = _SIMPLE_ACTIONS.get(action, action.value)
    if status is ActionStatus.SENT:
        detail = f" ({_reason(outcome.reason)})" if outcome.reason else ""
        return f"⏭ Не отправляли: правило без ответа — {label}: выполнено{detail}"
    return f"❌ Не удалось: {label} — {_reason(outcome.reason or status_ru(status))}"


def _sent_block(outcome: ReplyOutcome) -> str:
    dm_failed = bool(outcome.dm_text and outcome.dm_error)
    if dm_failed:
        # В группу ушло только «Отправлю в лс», сам ответ до лида не дошёл:
        # это не успех, и оператору нужен текст, чтобы дописать вручную.
        head = f"{DM_FAILED_HEAD}: {_esc(clip(outcome.dm_error or '', MAX_REASON_CHARS))}"
    else:
        head = SENT_HEAD
    notes: list[str] = []
    if outcome.delay_seconds:
        notes.append(f"после паузы {format_duration(outcome.delay_seconds)}")
    if outcome.reason == "duplicate":
        notes.append("повтор: этот ответ уже уходил раньше")
    if notes:
        head += f" ({', '.join(notes)})"
    head += _confidence(outcome)
    parts = [head]
    lead = _lead_line(outcome, "🎯 Лид")
    if lead is not None:
        parts.append(lead)
    reply = (outcome.reply_text or "").strip()
    if reply:
        title = "В чат" if outcome.dm_text else "Наш ответ"
        parts += [f"<b>{title}:</b>", _esc(clip(reply, MAX_REPLY_CHARS))]
    if outcome.dm_text:
        title = "В личку (не доставлено)" if dm_failed else "В личку"
        parts += [f"<b>{title}:</b>", _esc(clip(outcome.dm_text.strip(), MAX_REPLY_CHARS))]
    return "\n".join(parts)


def crash_line(exc: BaseException) -> str:
    """Конвейер упал на этом сообщении — ответа не было."""
    detail = clip(f"{type(exc).__name__}: {exc}", MAX_REASON_CHARS)
    return f"❌ Не удалось отправить: обработка упала — {_esc(detail)}"


# --- карточка и очередь -------------------------------------------------------------
class CardState(StrEnum):
    QUEUED = "queued"  # ждёт отправки в очереди
    POSTING = "posting"  # отправляется прямо сейчас
    POSTED = "posted"  # в группе, есть message_id
    HELD = "held"  # режим «каждое совпадение» выключен: ждём, ушёл ли ответ
    FAILED = "failed"  # отправить не вышло — итог попробуем прислать новой карточкой
    SKIPPED = "skipped"  # уведомления выключены или (режим выключен) ответа не было
    DROPPED = "dropped"  # выброшена из переполненной очереди


@dataclass(eq=False)
class MatchCard:
    """Карточка одного совпадения. Живёт вместе с обработкой сообщения.

    Конвейер зовёт только report()/report_line(): они синхронные и не
    поднимают исключений. Остальное — состояние фоновой задачи.
    """

    reporter: MatchLogReporter = field(repr=False)
    rule_id: uuid.UUID | None
    rule_name: str
    message: NormalizedMessage = field(repr=False)
    also_matched: tuple[str, ...] = ()
    result: str | None = None
    final: bool = False
    # Итог — отправленный ответ: карточка уйдёт и при выключенном режиме
    # «каждое совпадение», а для своего топика — ещё строка в общий поток.
    replied: bool = False
    # Заголовок строки в общий поток (✅ / ⚠️ личка не ушла).
    note_head: str | None = None
    # Ответ на проверке: id PendingReview — туда пишем id карточки.
    review_id: uuid.UUID | None = None
    state: CardState = CardState.QUEUED
    target: LogTarget | None = field(default=None, repr=False)
    account_label: str | None = None
    message_id: int | None = None
    # Итог, который сейчас виден в группе (в карточке или ответом на неё).
    shown_result: str | None = None
    update_queued: bool = False
    # Правка невозможна (сообщение удалено/устарело) — дальше итог ответом.
    edit_broken: bool = False
    # Повторы после временных сбоев (сбрасываются после удачного вызова).
    attempts: int = 0
    mirrored: bool = False
    review_linked: bool = False

    def report(self, outcome: ReplyOutcome) -> None:
        """Дописывает итог обработки (ReplyOutcome) в карточку."""
        try:
            line = outcome_line(outcome, tz_offset_hours=self.reporter.tz_offset_hours)
            replied = outcome.replied
            review_id = (
                outcome.review_id
                if outcome.action is ActionType.REQUEST_REVIEW
                and outcome.status is ActionStatus.SENT
                else None
            )
            note_head = DM_FAILED_HEAD if outcome.dm_text and outcome.dm_error else SENT_HEAD
        except Exception:  # карточка не должна ронять конвейер
            logger.exception("match_card_format_failed")
            action = getattr(getattr(outcome, "action", None), "value", "?")
            status = getattr(getattr(outcome, "status", None), "value", "?")
            line = f"❓ Итог: {_esc(str(action))} / {_esc(str(status))}"
            replied, review_id, note_head = False, None, None
        try:
            final = not outcome.scheduled
        except Exception:  # noqa: BLE001
            final = True
        self._apply(line, final=final, replied=replied, review_id=review_id, note_head=note_head)

    def report_line(self, line: str, *, final: bool = True) -> None:
        """Дописывает готовую строку итога. «⏳» не перетирает итог."""
        self._apply(line, final=final)

    def _apply(
        self,
        line: str,
        *,
        final: bool,
        replied: bool = False,
        review_id: uuid.UUID | None = None,
        note_head: str | None = None,
    ) -> None:
        if self.final and not final:
            return
        self.result = line
        self.final = final
        self.replied = replied and final
        self.note_head = note_head if self.replied else None
        if review_id is not None:
            self.review_id = review_id
        self.reporter._result_changed(self)


@dataclass(frozen=True, slots=True)
class _Op:
    # post — отправить карточку; update — дописать итог (правка или ответ);
    # mirror — строка об ответе в общий поток «Общение ИИ».
    kind: Literal["post", "update", "mirror"]
    card: MatchCard


class MatchLogReporter:
    """Очередь карточек совпадений и фоновая отправка в Bot API."""

    def __init__(
        self,
        database: Database,
        notifier: NotifierBot,
        *,
        tz_offset_hours: int = 0,
        queue_limit: int = DEFAULT_QUEUE_LIMIT,
        limiter: ChatRateLimiter | None = None,
    ) -> None:
        self._database = database
        self._notifier = notifier
        self.tz_offset_hours = tz_offset_hours
        self._queue_limit = max(queue_limit, 1)
        # По умолчанию — общий лимитёр бота: карточки ревью, лидов и дайджест
        # отмечаются в нём, и карточки совпадений им уступают.
        self._limiter = limiter if limiter is not None else notifier.limiter
        self._ops: deque[_Op] = deque()
        self._wakeup = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        # Счётчики для журнала: потери не бывают молчаливыми.
        self.dropped_cards = 0
        self.dropped_updates = 0
        self.dropped_notes = 0
        self.throttled = 0
        self.requeued = 0
        self.failed = 0
        self.loop_guarded = 0

    # --- вызывается из конвейера (синхронно, без исключений) -------------------
    def open_card(
        self,
        message: NormalizedMessage,
        match: RuleMatch,
        *,
        also_matched: Sequence[str] = (),
    ) -> MatchCard | None:
        """Ставит в очередь карточку совпадения и возвращает ссылку на неё."""
        try:
            if self._closed:
                # Воркер уже останавливается: карточки не будет — но не молча.
                self.dropped_cards += 1
                logger.warning(
                    "match_log_closed_dropped",
                    kind="post",
                    rule=match.rule.name,
                    dropped_cards=self.dropped_cards,
                )
                return None
            card = MatchCard(
                reporter=self,
                rule_id=match.rule.id,
                rule_name=match.rule.name,
                message=message,
                also_matched=tuple(also_matched),
            )
            self._enqueue(_Op("post", card))
        except Exception:  # лог-чат вторичен, ответ важнее
            logger.exception("match_card_open_failed", **message.for_log())
            return None
        return card

    def _result_changed(self, card: MatchCard) -> None:
        try:
            # Карточка ещё в очереди — уйдёт сразу с итогом, отдельная правка
            # не нужна. Выключено/выброшено — править нечего.
            if card.state in (CardState.QUEUED, CardState.SKIPPED, CardState.DROPPED):
                return
            if card.state is CardState.HELD:
                # Режим «каждое совпадение» выключен: ждём итога и шлём
                # карточку, только если ответ отправлен (как было раньше).
                if not card.final:
                    return
                if card.replied:
                    card.state = CardState.QUEUED
                    self._enqueue(_Op("post", card))
                else:
                    card.state = CardState.SKIPPED
                return
            if card.update_queued:
                return  # задание уже в очереди и возьмёт свежий итог
            self._enqueue(_Op("update", card))
        except Exception:  # лог-чат вторичен
            logger.exception("match_card_update_failed")

    def _enqueue(self, op: _Op) -> None:
        if self._closed:
            if op.kind == "post":
                op.card.state = CardState.DROPPED
                self.dropped_cards += 1
            elif op.kind == "update":
                self.dropped_updates += 1
            else:
                self.dropped_notes += 1
            logger.warning("match_log_closed_dropped", kind=op.kind, rule=op.card.rule_name)
            return
        if len(self._ops) >= self._queue_limit:
            self._drop_oldest()
        self._ops.append(op)
        if op.kind == "update":
            op.card.update_queued = True
        self._wakeup.set()
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run(), name="match-log")

    def _drop_oldest(self) -> None:
        """Переполнение: выбрасываем наименее ценное из самого старого.

        Сначала строку в общий поток (карточка с итогом уже в топике правила),
        потом самую старую неотправленную карточку, а если в очереди одни
        правки — самую старую правку (карточка останется с прежним итогом).
        """
        index = next((i for i, op in enumerate(self._ops) if op.kind == "mirror"), None)
        if index is None:
            index = next((i for i, op in enumerate(self._ops) if op.kind == "post"), 0)
        victim = self._ops[index]
        del self._ops[index]
        if victim.kind == "post":
            victim.card.state = CardState.DROPPED
            self.dropped_cards += 1
        elif victim.kind == "update":
            victim.card.update_queued = False
            self.dropped_updates += 1
        else:
            self.dropped_notes += 1
        logger.warning(
            "match_log_queue_overflow",
            dropped_kind=victim.kind,
            rule=victim.card.rule_name,
            dropped_cards=self.dropped_cards,
            dropped_updates=self.dropped_updates,
            dropped_notes=self.dropped_notes,
            queue_limit=self._queue_limit,
        )

    # --- остановка ------------------------------------------------------------
    async def close(self, grace_seconds: float = DEFAULT_CLOSE_GRACE_SECONDS) -> None:
        """Даёт очереди дойти до конца в пределах grace_seconds и гасит задачу."""
        self._closed = True
        self._wakeup.set()
        task = self._task
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=grace_seconds)
        except TimeoutError:
            left = len(self._ops)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            logger.warning("match_log_unsent_on_shutdown", pending=left)

    @property
    def pending(self) -> int:
        return len(self._ops)

    # --- фоновая задача -------------------------------------------------------
    async def _run(self) -> None:
        while True:
            if not self._ops:
                if self._closed:
                    return
                self._wakeup.clear()
                await self._wakeup.wait()
                continue
            op = self._ops.popleft()
            try:
                if op.kind == "post":
                    await self._post(op.card)
                elif op.kind == "update":
                    op.card.update_queued = False
                    await self._update(op.card)
                else:
                    await self._mirror(op.card)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — одна карточка не роняет очередь
                self.failed += 1
                logger.warning("match_log_failed", kind=op.kind, detail=str(exc)[:200])

    def _requeue(
        self,
        card: MatchCard,
        kind: Literal["post", "update", "mirror"],
        exc: BaseException,
        *,
        any_error: bool = False,
    ) -> bool:
        """Временный сбой: задание — в конец очереди (не больше MAX_REQUEUES раз).

        False — повторять не будем (сбой не временный или попытки кончились).
        """
        if not (any_error or is_transient(exc)) or card.attempts >= MAX_REQUEUES:
            return False
        card.attempts += 1
        self.requeued += 1
        logger.warning(
            "match_log_requeued",
            kind=kind,
            attempt=card.attempts,
            requeued=self.requeued,
            detail=str(exc)[:200],
        )
        if kind == "post":
            card.state = CardState.QUEUED
        elif kind == "update" and card.update_queued:
            return True  # свежий итог уже в очереди — он и уйдёт
        self._enqueue(_Op(kind, card))
        return True

    async def _post(self, card: MatchCard) -> None:
        if card.state is not CardState.QUEUED:
            return
        card.state = CardState.POSTING
        if card.target is None:
            try:
                resolved = await self._resolve(card)
            except Exception as exc:  # noqa: BLE001 — база/Bot API недоступны
                if self._requeue(card, "post", exc, any_error=True):
                    return
                card.state = CardState.FAILED
                self.failed += 1
                logger.warning("match_log_target_failed", detail=str(exc)[:200])
                return
            if resolved is None:
                card.state = CardState.SKIPPED
                return
        if not self._wanted(card):
            return
        await self._post_new(card)

    def _wanted(self, card: MatchCard) -> bool:
        """Режим «каждое совпадение» выключен — карточка только об ответе."""
        assert card.target is not None
        if card.target.all_matches or (card.final and card.replied):
            return True
        card.state = CardState.SKIPPED if card.final else CardState.HELD
        return False

    async def _post_new(self, card: MatchCard) -> None:
        """Отправляет карточку целиком (с текущим итогом) новым сообщением."""
        assert card.target is not None
        target = card.target
        shown = card.result
        text = self._render(card)
        try:
            message_id = await self._with_retry(
                target, lambda: self._notifier.send_log_card(target, text)
            )
        except Exception as exc:  # noqa: BLE001 — итог попробуем прислать позже
            # Таймаут чтения: карточка могла и дойти — тогда повтор даст
            # дубль. Лучше дубль, чем совпадение, пропавшее из лог-чата.
            if self._requeue(card, "post", exc):
                return
            card.state = CardState.FAILED
            self.failed += 1
            logger.warning("match_card_send_failed", detail=str(exc)[:200])
            await self._record_error(f"карточка совпадения: {exc}")
            return
        card.message_id = message_id
        card.state = CardState.POSTED
        await self._shown(card, shown)

    async def _update(self, card: MatchCard) -> None:
        if card.state in (
            CardState.QUEUED,
            CardState.SKIPPED,
            CardState.DROPPED,
            CardState.HELD,
        ):
            return
        if card.result is None or card.result == card.shown_result:
            return  # итог уже виден (ушёл вместе с карточкой)
        if card.state is CardState.FAILED or card.message_id is None:
            # Карточку отправить не вышло — итог уходит новой карточкой целиком.
            if card.target is None:
                try:
                    if await self._resolve(card) is None:
                        card.state = CardState.SKIPPED
                        return
                except Exception as exc:  # noqa: BLE001
                    if not self._requeue(card, "update", exc, any_error=True):
                        self.failed += 1
                        logger.warning("match_log_target_failed", detail=str(exc)[:200])
                    return
            if self._wanted(card):
                await self._post_new(card)
            return

        target = card.target
        assert target is not None
        message_id = card.message_id
        shown = card.result
        if not card.edit_broken:
            text = self._render(card)
            try:
                await self._with_retry(
                    target, lambda: self._notifier.edit_log_card(target, message_id, text)
                )
            except Exception as exc:  # noqa: BLE001
                if self._requeue(card, "update", exc):
                    return  # 429/5xx/сеть — правку повторим (уже ушедшая даст «not modified»)
                if is_transient(exc):
                    self.failed += 1
                    logger.warning("match_card_edit_gave_up", detail=str(exc)[:200])
                    await self._record_error(f"итог карточки совпадения: {exc}")
                    return
                if is_uneditable(exc):
                    card.edit_broken = True  # дальше итог сразу ответом
                logger.info("match_card_edit_failed_reply_instead", detail=str(exc)[:200])
            else:
                await self._shown(card, shown)
                return
        try:
            await self._with_retry(
                target,
                lambda: self._notifier.send_log_card(target, shown, reply_to=message_id),
            )
        except Exception as exc:  # noqa: BLE001
            if self._requeue(card, "update", exc):
                return
            self.failed += 1
            logger.warning("match_card_result_failed", detail=str(exc)[:200])
            await self._record_error(f"итог карточки совпадения: {exc}")
            return
        await self._shown(card, shown)

    async def _shown(self, card: MatchCard, shown: str | None) -> None:
        """Итог дошёл до группы: общий поток и ссылка у ревью — по разу."""
        card.shown_result = shown
        card.attempts = 0
        if not card.final or shown != card.result:
            return  # свежий итог ещё в пути
        target = card.target
        if (
            card.replied
            and not card.mirrored
            and target is not None
            and target.stream_thread_id is not None
            and card.message_id is not None
        ):
            card.mirrored = True
            self._enqueue(_Op("mirror", card))
        if card.review_id is not None and not card.review_linked and card.message_id is not None:
            await self._link_review(card)

    async def _mirror(self, card: MatchCard) -> None:
        """Строка об отправленном ответе в «Общение ИИ» со ссылкой на карточку."""
        target = card.target
        if target is None or target.stream_thread_id is None or card.message_id is None:
            return
        stream = LogTarget(
            token=target.token, group_id=target.group_id, thread_id=target.stream_thread_id
        )
        text = format_stream_note(
            head=card.note_head or SENT_HEAD,
            rule_name=card.rule_name,
            message=card.message,
            card_link=message_link(target.group_id, card.message_id, None),
        )
        try:
            await self._with_retry(stream, lambda: self._notifier.send_log_card(stream, text))
        except Exception as exc:  # noqa: BLE001
            if self._requeue(card, "mirror", exc):
                return
            self.failed += 1
            logger.warning("match_card_stream_note_failed", detail=str(exc)[:200])

    async def _link_review(self, card: MatchCard) -> None:
        """Ответ на проверке: id карточки — в PendingReview, чтобы решение
        оператора (кнопки, панель) дописалось к ней, а не повисло «на проверке»."""
        assert card.target is not None
        try:
            async with self._database.session() as db:
                await db.execute(
                    update(PendingReview)
                    .where(PendingReview.id == card.review_id)
                    .values(
                        match_card_message_id=card.message_id,
                        match_card_thread_id=card.target.thread_id,
                    )
                )
            card.review_linked = True
        except Exception as exc:  # noqa: BLE001 — без ссылки просто не будет пометки
            logger.warning("match_card_review_link_failed", detail=str(exc)[:200])

    async def _with_retry(self, target: LogTarget, call: Callable[[], Awaitable[T]]) -> T:
        """Вызов Bot API под лимитом; на 429 — ждём retry_after и ещё одна попытка."""
        for attempt in (1, 2):
            await self._limiter.acquire(target.group_id)
            try:
                return await call()
            except NotifyRateLimitError as exc:
                self.throttled += 1
                # Лимитёр подождёт retry_after перед следующей попыткой — и
                # задержит все следующие карточки этой группы.
                self._limiter.block(target.group_id, exc.retry_after)
                logger.warning(
                    "match_log_throttled",
                    retry_after=exc.retry_after,
                    attempt=attempt,
                    throttled=self.throttled,
                )
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")  # pragma: no cover

    async def _resolve(self, card: MatchCard) -> LogTarget | None:
        """Куда слать (и слать ли вообще) + подпись аккаунта."""
        async with self._database.session() as db:
            target = await self._notifier.log_target(db, rule_id=card.rule_id)
            if target is None:
                return None
            if is_own_log_traffic(card.message, target):
                # Защита от петли: аккаунт состоит в лог-чате и видит наши же
                # карточки; правило срабатывает на их текст → новая карточка →
                # снова совпадение… (авария 29.09: 73 сообщения за 4 минуты).
                self.loop_guarded += 1
                logger.warning("match_log_loop_guard", loop_guarded=self.loop_guarded)
                return None
            account = await db.get(Account, card.message.account_id)
        card.target = target
        card.account_label = _account_label(account, card.message.account_id)
        return target

    def _render(self, card: MatchCard) -> str:
        message = card.message
        return format_match_card(
            rule_name=card.rule_name,
            account_label=card.account_label or str(message.account_id)[:8],
            chat_title=message.chat_title,
            chat_username=message.chat_username,
            tg_chat_id=message.tg_chat_id,
            tg_message_id=message.tg_message_id,
            is_private=message.is_private,
            sender_name=message.sender_display_name,
            sender_username=message.sender_username,
            sender_tg_id=message.sender_tg_id,
            incoming_text=message.text,
            date=message.date,
            tz_offset_hours=self.tz_offset_hours,
            result=card.result,
            also_matched=card.also_matched,
        )

    async def _record_error(self, detail: str) -> None:
        with contextlib.suppress(Exception):
            async with self._database.session() as db:
                await self._notifier.record_error(db, detail[:300])


def _account_label(account: Account | None, account_id: uuid.UUID) -> str:
    if account is None:
        return str(account_id)[:8]
    if account.label:
        return account.label
    if account.username:
        return f"@{account.username}"
    return str(account_id)[:8]
