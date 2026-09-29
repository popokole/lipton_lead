"""Карточки совпадений правил в лог-чате (форум-группа бота-уведомителя).

Владелец хочет видеть в лог-чате КАЖДОЕ сообщение, совпавшее с правилом, а не
только те, на которые ушёл ответ. Поэтому:

1. Сразу после совпадения (после claim() в MonitorPipeline — одно и то же
   сообщение не попадёт дважды, даже если его довыгрузит реконсайлер) ставим
   в очередь карточку: правило, аккаунт, чат и ссылка, отправитель, текст,
   время. Маршрут — как у notify_lead: свой топик правила, если включён,
   иначе общий поток «Общение ИИ».
2. Когда итог известен, та же карточка редактируется (editMessageText): ✅
   ответ ушёл (текст ответа и лички) / ❌ не удалось отправить / ⏭ не
   отправляли и почему / 🟡 на проверке у оператора / ⏳ ответ запланирован
   через N с (потом — итог). Если отредактировать не вышло (сообщение удалено,
   слишком старое) — итог уходит ответом на карточку в тот же топик.

Всё это вторично по отношению к ответу лиду: конвейер только кладёт задание в
очередь (синхронно, без await и без исключений), а в Bot API ходит одна
фоновая задача. Она соблюдает лимит Telegram (~20 сообщений в минуту на
группу, см. ChatRateLimiter) и retry_after из 429: ждёт и повторяет один раз.
Если очередь переполнена, выбрасываются самые старые ещё не отправленные
карточки — с предупреждением в журнале и счётчиком, а не молча.

Ссылка на карточку (MatchCard) идёт вместе с обработкой сообщения:
MonitorPipeline → ReplyPipeline.handle → _PreparedReply → фоновая задача
отложенного ответа, которая и дописывает итог после паузы. Хранится она только
в памяти: после жёсткого падения воркера карточка так и останется с «⏳», а
зависший ответ отдаст оператору sweep_stale_scheduled.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import re
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, TypeVar

from app.core.logging import get_logger
from app.database.session import Database
from app.models import Account, ActionStatus, ActionType
from app.notifications.notifier import (
    LogTarget,
    NotifierBot,
    NotifyRateLimitError,
    message_link,
)
from app.rules.engine import RuleMatch
from app.telegram.messages import NormalizedMessage

if TYPE_CHECKING:
    from app.pipeline.reply_pipeline import ReplyOutcome

logger = get_logger(__name__)

T = TypeVar("T")

#: Лимиты длины частей карточки — в UTF-16 (так считает Telegram). Сумма
#: с запасом меньше 4096: сообщение не упадёт с «message is too long».
MAX_INCOMING_CHARS = 700
MAX_REPLY_CHARS = 700
MAX_NAME_CHARS = 100
MAX_REASON_CHARS = 400
TELEGRAM_TEXT_LIMIT = 4096

#: Очередь заданий к Bot API. При переполнении выбрасываются самые старые
#: ещё не отправленные карточки (с предупреждением в журнале).
DEFAULT_QUEUE_LIMIT = 200
#: Telegram держит ~20 сообщений в минуту на группу; правки считаем так же.
#: Берём меньше — запас на карточки ревью, лидов и дайджест того же бота.
DEFAULT_MAX_PER_MINUTE = 18
#: И не чаще раза в секунду в один чат (общий лимит Bot API на чат).
DEFAULT_MIN_INTERVAL_SECONDS = 1.0
#: Сколько при остановке воркера ждём, пока очередь дойдёт до конца.
DEFAULT_CLOSE_GRACE_SECONDS = 5.0

PENDING_LINE = "⌛ Обрабатываю…"
STOPLIST_LINE = "⏭ Не отправляли: отправитель в стоп-листе"
NO_REPLY_PIPELINE_LINE = "⏭ Не отправляли: ответы на этом воркере выключены"


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
) -> str:
    """Карточка совпадения: правило, кто, откуда, когда, что написал, итог.

    result — уже готовый HTML итога (см. outcome_line); None — итог ещё не
    известен. Всё пользовательское экранируется и обрезается.
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

    lines = [
        f"🔔 <b>Совпадение</b> · {_esc(clip(rule_name, MAX_NAME_CHARS))}",
        sender_line,
        where_line,
    ]
    if date is not None:
        local = date + timedelta(hours=tz_offset_hours)
        lines.append(f"🕒 {local:%d.%m %H:%M:%S}")
    text = incoming_text.strip() or "(без текста)"
    lines += ["", "<b>Сообщение:</b>", _esc(clip(text, MAX_INCOMING_CHARS))]
    lines += ["", result or PENDING_LINE]
    return "\n".join(lines)


# --- итог обработки → строка карточки -------------------------------------------
_COOLDOWN_SCOPES = {
    "user": "этому человеку недавно уже отвечали",
    "chat": "в этот чат недавно уже отвечали",
    "account": "аккаунт недавно уже отвечал",
    "rule": "правило недавно уже срабатывало",
    "scenario": "сценарий недавно уже отвечал",
}
_AI_SKIP = re.compile(r"^AI: не отвечать \(confidence ([\d.]+), порог ([\d.]+)\)$")
_EXACT_REASONS = {
    "анти-бан: лимит на чат": "анти-бан: в этот чат недавно уже отвечали",
    "вне рабочих часов": "вне рабочих часов",
    "one_shot: уже связались": "«один заход»: с этим человеком уже связывались",
    "one_shot: ответ уже запланирован": "«один заход»: ответ этому человеку уже запланирован",
}
_SIMPLE_ACTIONS = {
    ActionType.NOTIFY_ADMIN: "уведомление в панель",
    ActionType.SAVE_LEAD: "сохранить лида",
    ActionType.TAG_USER: "метка собеседнику",
}


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
    ai = _AI_SKIP.match(reason)
    if ai:
        return f"ИИ решил не отвечать (уверенность {ai.group(1)} при пороге {ai.group(2)})"
    return reason


def _reason(text: str | None) -> str:
    return _esc(clip(humanize_reason(text), MAX_REASON_CHARS))


def _confidence(outcome: ReplyOutcome) -> str:
    analysis = outcome.analysis
    if analysis is None or analysis.failed:
        return ""
    return f" · уверенность ИИ {analysis.result.confidence:.2f}"


def _seconds(value: float | None) -> str:
    return f"{value:.0f} с" if value is not None else "? с"


def outcome_line(outcome: ReplyOutcome) -> str:
    """Итог обработки совпадения — готовый (экранированный) HTML для карточки.

    ✅ ответ ушёл · ❌ отправка не удалась · ⏭ не отправляли и почему ·
    🟡 на проверке у оператора · ⏳ ответ запланирован.
    """
    action, status = outcome.action, outcome.status
    handed_over = " · передано оператору" if action is ActionType.ESCALATE_TO_HUMAN else ""

    if outcome.send_error is not None:
        return f"❌ Не удалось отправить: {_reason(outcome.send_error)}{handed_over}"

    if action is ActionType.REPLY:
        if status is ActionStatus.PENDING:
            delay = _seconds(outcome.delay_seconds)
            return f"⏳ Ответ запланирован через {delay}{_confidence(outcome)}"
        if status is not ActionStatus.SENT:
            return f"❌ Не удалось отправить: {_reason(outcome.reason or status.value)}"
        return _sent_block(outcome)

    if action is ActionType.REQUEST_REVIEW:
        if status is ActionStatus.SENT:
            return (
                "🟡 Не отправляли: ответ на проверке у оператора "
                f"(карточка с кнопками — в «Общение ИИ»){_confidence(outcome)}"
            )
        return f"❌ Не удалось отдать ответ на проверку оператору: {_reason(outcome.reason)}"

    if action is ActionType.IGNORE:
        line = f"⏭ Не отправляли: {_reason(outcome.reason)}"
        if status is not ActionStatus.SENT:
            line += f" (⚠️ отметка в журнале: {status.value})"
        return line

    if action is ActionType.ESCALATE_TO_HUMAN:
        if status is ActionStatus.SENT:
            return f"⏭ Не отправляли: {_reason(outcome.reason)} · передано оператору"
        return (
            f"⏭ Не отправляли: {_reason(outcome.reason)} · "
            f"⚠️ передать оператору не удалось ({status.value})"
        )

    label = _SIMPLE_ACTIONS.get(action, action.value)
    if status is ActionStatus.SENT:
        detail = f" ({_esc(clip(outcome.reason, MAX_REASON_CHARS))})" if outcome.reason else ""
        return f"⏭ Не отправляли: правило без ответа — {label}: выполнено{detail}"
    return f"❌ Не удалось: {label} — {_reason(outcome.reason or status.value)}"


def _sent_block(outcome: ReplyOutcome) -> str:
    head = "✅ Ответ отправлен"
    if outcome.delay_seconds:
        head += f" (после паузы {_seconds(outcome.delay_seconds)})"
    if outcome.reason == "duplicate":
        head += " (повтор: этот ответ уже уходил раньше)"
    head += _confidence(outcome)
    parts = [head]
    reply = (outcome.reply_text or "").strip()
    if reply:
        title = "В чат" if outcome.dm_text else "Наш ответ"
        parts += [f"<b>{title}:</b>", _esc(clip(reply, MAX_REPLY_CHARS))]
    if outcome.dm_text:
        if outcome.dm_error:
            parts.append(f"⚠️ Личка не ушла: {_esc(clip(outcome.dm_error, MAX_REASON_CHARS))}")
        else:
            parts += ["<b>В личку:</b>", _esc(clip(outcome.dm_text.strip(), MAX_REPLY_CHARS))]
    return "\n".join(parts)


def crash_line(exc: BaseException) -> str:
    """Конвейер упал на этом сообщении — ответа не было."""
    detail = clip(f"{type(exc).__name__}: {exc}", MAX_REASON_CHARS)
    return f"❌ Не удалось отправить: обработка упала — {_esc(detail)}"


# --- лимит Telegram ----------------------------------------------------------------
class ChatRateLimiter:
    """Не больше max_per_window сообщений/правок в чат за window секунд, не
    чаще раза в min_interval и — после 429 — ничего до конца retry_after.

    Один потребитель (фоновая задача MatchLogReporter), поэтому без блокировок.
    Часы и сон подменяются в тестах.
    """

    def __init__(
        self,
        *,
        max_per_window: int = DEFAULT_MAX_PER_MINUTE,
        window_seconds: float = 60.0,
        min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._max = max(max_per_window, 1)
        self._window = window_seconds
        self._min_interval = min_interval_seconds
        self._clock = clock
        self._sleep = sleep
        self._sent: dict[int, deque[float]] = {}
        self._blocked_until: dict[int, float] = {}

    def delay(self, chat_id: int) -> float:
        """Сколько секунд ждать до следующей отправки в этот чат."""
        now = self._clock()
        wait = self._blocked_until.get(chat_id, 0.0) - now
        sent = self._sent.get(chat_id)
        if sent:
            while sent and sent[0] <= now - self._window:
                sent.popleft()
            if sent:
                wait = max(wait, sent[-1] + self._min_interval - now)
                if len(sent) >= self._max:
                    wait = max(wait, sent[0] + self._window - now)
        return max(wait, 0.0)

    async def acquire(self, chat_id: int) -> None:
        """Ждёт свободного места в окне и занимает его."""
        while (wait := self.delay(chat_id)) > 0:
            await self._sleep(wait)
        self._sent.setdefault(chat_id, deque()).append(self._clock())

    def block(self, chat_id: int, seconds: float) -> None:
        """429: в этот чат ничего не шлём ближайшие seconds секунд."""
        until = self._clock() + max(seconds, 0.0)
        self._blocked_until[chat_id] = max(self._blocked_until.get(chat_id, 0.0), until)


# --- карточка и очередь -------------------------------------------------------------
class CardState(StrEnum):
    QUEUED = "queued"  # ждёт отправки в очереди
    POSTING = "posting"  # отправляется прямо сейчас
    POSTED = "posted"  # в группе, есть message_id
    FAILED = "failed"  # отправить не вышло — итог попробуем прислать новой карточкой
    SKIPPED = "skipped"  # уведомления или режим «каждое совпадение» выключены
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
    result: str | None = None
    final: bool = False
    state: CardState = CardState.QUEUED
    target: LogTarget | None = field(default=None, repr=False)
    account_label: str | None = None
    message_id: int | None = None
    # Итог, который сейчас виден в группе (в карточке или ответом на неё).
    shown_result: str | None = None
    update_queued: bool = False
    # Правка не удалась раз — дальше итог сразу ответом на карточку.
    edit_broken: bool = False

    def report(self, outcome: ReplyOutcome) -> None:
        """Дописывает итог обработки (ReplyOutcome) в карточку."""
        try:
            line = outcome_line(outcome)
        except Exception:  # карточка не должна ронять конвейер
            logger.exception("match_card_format_failed")
            line = f"❓ Итог: {_esc(outcome.action.value)} / {_esc(outcome.status.value)}"
        self.report_line(line, final=not outcome.scheduled)

    def report_line(self, line: str, *, final: bool = True) -> None:
        """Дописывает готовую строку итога. «⏳» не перетирает итог."""
        if self.final and not final:
            return
        self.result = line
        self.final = final
        self.reporter._result_changed(self)


@dataclass(frozen=True, slots=True)
class _Op:
    kind: Literal["post", "update"]
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
        self._tz_offset = tz_offset_hours
        self._queue_limit = max(queue_limit, 1)
        self._limiter = limiter or ChatRateLimiter()
        self._ops: deque[_Op] = deque()
        self._wakeup = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        # Счётчики для журнала: потери не бывают молчаливыми.
        self.dropped_cards = 0
        self.dropped_updates = 0
        self.throttled = 0
        self.failed = 0

    # --- вызывается из конвейера (синхронно, без исключений) -------------------
    def open_card(self, message: NormalizedMessage, match: RuleMatch) -> MatchCard | None:
        """Ставит в очередь карточку совпадения и возвращает ссылку на неё."""
        if self._closed:
            return None
        try:
            card = MatchCard(
                reporter=self,
                rule_id=match.rule.id,
                rule_name=match.rule.name,
                message=message,
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
            if card.update_queued:
                return  # задание уже в очереди и возьмёт свежий итог
            self._enqueue(_Op("update", card))
        except Exception:  # лог-чат вторичен
            logger.exception("match_card_update_failed")

    def _enqueue(self, op: _Op) -> None:
        if self._closed:
            if op.kind == "post":
                self.dropped_cards += 1
            else:
                self.dropped_updates += 1
            logger.warning("match_log_closed_dropped", kind=op.kind)
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
        """Переполнение: выбрасываем самую старую неотправленную карточку.

        Если в очереди одни правки уже отправленных карточек — самую старую
        правку (карточка останется с прежним итогом).
        """
        index = next((i for i, op in enumerate(self._ops) if op.kind == "post"), 0)
        victim = self._ops[index]
        del self._ops[index]
        if victim.kind == "post":
            victim.card.state = CardState.DROPPED
            self.dropped_cards += 1
        else:
            victim.card.update_queued = False
            self.dropped_updates += 1
        logger.warning(
            "match_log_queue_overflow",
            dropped_kind=victim.kind,
            rule=victim.card.rule_name,
            dropped_cards=self.dropped_cards,
            dropped_updates=self.dropped_updates,
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
                else:
                    op.card.update_queued = False
                    await self._update(op.card)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — одна карточка не роняет очередь
                self.failed += 1
                logger.warning("match_log_failed", kind=op.kind, detail=str(exc)[:200])

    async def _post(self, card: MatchCard) -> None:
        if card.state is not CardState.QUEUED:
            return
        card.state = CardState.POSTING
        try:
            resolved = await self._resolve(card)
        except Exception as exc:  # noqa: BLE001 — база/Bot API недоступны
            card.state = CardState.FAILED
            self.failed += 1
            logger.warning("match_log_target_failed", detail=str(exc)[:200])
            return
        if resolved is None:
            card.state = CardState.SKIPPED
            return
        await self._post_new(card)

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
            card.state = CardState.FAILED
            self.failed += 1
            logger.warning("match_card_send_failed", detail=str(exc)[:200])
            await self._record_error(f"карточка совпадения: {exc}")
            return
        card.message_id = message_id
        card.shown_result = shown
        card.state = CardState.POSTED

    async def _update(self, card: MatchCard) -> None:
        if card.state in (CardState.QUEUED, CardState.SKIPPED, CardState.DROPPED):
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
                    logger.warning("match_log_target_failed", detail=str(exc)[:200])
                    return
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
                card.shown_result = shown
                return
            except NotifyRateLimitError as exc:
                # Лимит не отпустил и после повтора: ответом было бы то же самое.
                self.failed += 1
                logger.warning("match_card_edit_throttled", detail=str(exc)[:200])
                return
            except Exception as exc:  # noqa: BLE001 — старое/удалённое сообщение
                card.edit_broken = True
                logger.info("match_card_edit_failed_reply_instead", detail=str(exc)[:200])
        try:
            await self._with_retry(
                target,
                lambda: self._notifier.send_log_card(target, shown, reply_to=message_id),
            )
            card.shown_result = shown
        except Exception as exc:  # noqa: BLE001
            self.failed += 1
            logger.warning("match_card_result_failed", detail=str(exc)[:200])
            await self._record_error(f"итог карточки совпадения: {exc}")

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
            tz_offset_hours=self._tz_offset,
            result=card.result,
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
