"""Бот-уведомления: отчёты о лидах в форум-группу (ТЗ §36).

Отдельный бот (токен @BotFather) шлёт карточку лида в топик. По умолчанию все
ответы и карточки идут в один общий топик «Общение ИИ» (`notify_settings.ai_chat_topic_id`);
правило со включённым `notify_topic_enabled` получает СВОЙ топик, который
создаётся лениво один раз и запоминается в `rule.notify_topic_id`.

Это Bot API (api.telegram.org), а не пользовательский клиент Telethon —
поэтому обычный httpx, без сессий и аренды. Работает в воркере рядом с
отправкой ответа, но полностью изолирован: любой сбой бота не должен влиять
на сам ответ лиду.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import EncryptedBlob, SecretBox
from app.core.logging import get_logger
from app.models import Chat, Rule
from app.models.notify import SINGLETON_ID, NotifySettings

logger = get_logger(__name__)

API_BASE = "https://api.telegram.org"

#: Telegram держит ~20 сообщений в минуту на группу; правки считаем так же.
#: Карточкам совпадений берём меньше — запас на ревью, лиды и дайджест.
DEFAULT_MAX_PER_MINUTE = 18
#: И не чаще раза в секунду в один чат (общий лимит Bot API на чат).
DEFAULT_MIN_INTERVAL_SECONDS = 1.0
#: Вызовы Bot API, которые пишут в чат и потому считаются в лимит группы.
_RATE_LIMITED_METHODS = frozenset({"sendMessage", "editMessageText", "editMessageReplyMarkup"})
#: Карточку ревью (с кнопками) после 429 повторяем один раз, если Telegram
#: просит подождать не дольше этого: она шлётся прямо из обработки сообщения,
#: и долгий сон задержал бы обработчик (сам ответ лиду здесь не ждёт — его ещё
#: не отправляли, он на проверке).
REVIEW_RETRY_MAX_SECONDS = 10.0


class ChatRateLimiter:
    """Не больше max_per_window сообщений/правок в чат за window секунд, не
    чаще раза в min_interval и — после 429 — ничего до конца retry_after.

    Один на NotifierBot: все вызовы Bot API в группу отмечаются в нём (note),
    поэтому карточки совпадений (acquire — ждёт своей очереди) уступают место
    карточкам ревью, лидов и дайджесту, которые идут без ожидания. Лимит — на
    процесс: два воркера на одну группу вместе могут превысить его (тогда
    выручает 429 → retry_after). Часы и сон подменяются в тестах.
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
        self.note(chat_id)

    def note(self, chat_id: int) -> None:
        """Отмечает вызов, ушедший без ожидания (ревью, лид, дайджест)."""
        self._sent.setdefault(chat_id, deque()).append(self._clock())

    def block(self, chat_id: int, seconds: float) -> None:
        """429: в этот чат ничего не шлём ближайшие seconds секунд."""
        until = self._clock() + max(seconds, 0.0)
        self._blocked_until[chat_id] = max(self._blocked_until.get(chat_id, 0.0), until)


_CHECK_AI_KEYBOARD = {
    "inline_keyboard": [[{"text": "🔍 Проверить ИИ (codex.sale)", "callback_data": "check_ai"}]]
}

_START_MENU_KEYBOARD = {
    "inline_keyboard": [
        [{"text": "🔍 Проверить ИИ (codex.sale)", "callback_data": "check_ai"}],
    ]
}


class NotifierBot:
    """Отправка карточек лидов через Bot API форум-группы."""

    def __init__(
        self,
        box: SecretBox,
        proxy: str | None = None,
        *,
        limiter: ChatRateLimiter | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._box = box
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(20.0), proxy=proxy)
        # Общий на все вызовы в группу: карточки совпадений ждут в нём своей
        # очереди, остальные сообщения бота только отмечаются (см. _call).
        self.limiter = limiter or ChatRateLimiter()
        self._sleep = sleep

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(
        self, token: str, method: str, *, counted: bool = False, **params: Any
    ) -> dict[str, Any]:
        """Вызов Bot API.

        counted=True — место в лимите группы уже занято (ChatRateLimiter.acquire
        у MatchLogReporter); иначе вызов в группу просто отмечается в лимите.
        """
        raw_chat_id = params.get("chat_id")
        chat_id = raw_chat_id if isinstance(raw_chat_id, int) else None
        limited = method in _RATE_LIMITED_METHODS and chat_id is not None
        if chat_id is not None and limited and not counted:
            self.limiter.note(chat_id)
        resp = await self._client.post(f"{API_BASE}/bot{token}/{method}", json=params)
        try:
            data = resp.json()
        except ValueError as exc:  # 502 от прокси/балансера — HTML вместо JSON
            raise NotifyTransientError(
                f"Bot API {method}: HTTP {resp.status_code}, ответ не JSON"
            ) from exc
        if not data.get("ok"):
            description = data.get("description") or f"Bot API {method} failed"
            retry_after = (data.get("parameters") or {}).get("retry_after")
            error_code = data.get("error_code")
            if error_code == 429 or retry_after is not None:
                # Лимит Telegram (≈20 сообщений в минуту на группу): сколько ждать,
                # Bot API говорит сам — его и соблюдаем (см. MatchLogReporter).
                wait = float(retry_after or 1)
                if chat_id is not None and limited:
                    self.limiter.block(chat_id, wait)
                raise NotifyRateLimitError(description, retry_after=wait)
            if isinstance(error_code, int) and error_code >= 500:
                raise NotifyTransientError(description)
            raise NotifyError(description)
        return data["result"]

    async def check(self, token: str) -> str:
        """Проверяет токен, возвращает username бота."""
        me = await self._call(token, "getMe")
        return str(me.get("username") or "")

    async def sync_topics(self, db: AsyncSession, *, rename: bool) -> dict[str, Any]:
        """Готовит группу: общий топик «Общение ИИ» + свой топик у отмеченных правил.

        Проверяет, что группа — форум (иначе топики создать нельзя). Свой топик
        создаётся только для правил с `notify_topic_enabled=True`; остальные
        правила используют общий топик. При rename=True приводит имя топика
        правила к его текущему имени (editForumTopic).
        """
        loaded = await self._load_settings(db, require_enabled=False)
        if loaded is None:
            raise NotifyError("Сначала сохраните токен бота и id группы")
        token, group_id = loaded

        chat = await self._call(token, "getChat", chat_id=group_id)
        if not chat.get("is_forum"):
            raise NotifyError(
                "У группы не включены темы (Topics). Включите их в настройках "
                "группы и сделайте бота админом с правом управлять темами."
            )

        rules = list(
            (await db.scalars(select(Rule).where(Rule.notify_topic_enabled.is_(True)))).all()
        )
        created = existing = renamed = 0
        for rule in rules:
            if rule.notify_topic_id is None:
                topic = await self._call(
                    token, "createForumTopic", chat_id=group_id, name=rule.name[:128]
                )
                rule.notify_topic_id = int(topic["message_thread_id"])
                created += 1
            else:
                existing += 1
                if rename:
                    try:
                        await self._call(
                            token,
                            "editForumTopic",
                            chat_id=group_id,
                            message_thread_id=rule.notify_topic_id,
                            name=rule.name[:128],
                        )
                        renamed += 1
                    except NotifyError:
                        pass  # топик мог быть удалён вручную — не критично

        # Общий поток: все ответы и карточки на подтверждение, для которых
        # правило не завело свой топик.
        row = await db.get(NotifySettings, SINGLETON_ID)
        if row is not None:
            if row.ai_chat_topic_id is None:
                topic = await self._call(
                    token, "createForumTopic", chat_id=group_id, name="Общение ИИ"
                )
                row.ai_chat_topic_id = int(topic["message_thread_id"])
                created += 1
            if row.digest_topic_id is None:
                topic = await self._call(
                    token, "createForumTopic", chat_id=group_id, name="Дайджест"
                )
                row.digest_topic_id = int(topic["message_thread_id"])
                created += 1

        await db.flush()
        return {
            "is_forum": True,
            "rules_with_own_topic": len(rules),
            "created": created,
            "existing": existing,
            "renamed": renamed,
        }

    async def _ensure_stream_topic(self, db: AsyncSession, token: str, group_id: int) -> int | None:
        """id общего топика «Общение ИИ», создавая его при первом обращении."""
        row = await db.get(NotifySettings, SINGLETON_ID)
        if row is None:
            return None
        if row.ai_chat_topic_id is None:
            topic = await self._call(token, "createForumTopic", chat_id=group_id, name="Общение ИИ")
            row.ai_chat_topic_id = int(topic["message_thread_id"])
            await db.flush()
        return row.ai_chat_topic_id

    async def notify_stream(self, db: AsyncSession, *, is_private: bool, text: str) -> None:
        """Шлёт ответ в общий топик «Общение ИИ».

        Отдельно от notify_lead (свой топик правила, если включён): здесь
        копятся все наши ответы одним потоком. Никогда не поднимает
        исключение — уведомление вторично.
        """
        try:
            settings = await self._load_settings(db)
            if settings is None:
                return
            token, group_id = settings
            thread_id = await self._ensure_stream_topic(db, token, group_id)
            await self._call(
                token,
                "sendMessage",
                chat_id=group_id,
                message_thread_id=thread_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as exc:  # noqa: BLE001 — уведомление не критично
            logger.warning("notify_stream_failed", detail=str(exc)[:200])
            await self._record_error(db, str(exc)[:300])

    async def notify_lead(
        self,
        db: AsyncSession,
        *,
        rule_id: uuid.UUID | None,
        rule_name: str | None,
        notify_topic_enabled: bool = False,
        text: str,
    ) -> None:
        """Шлёт готовый текст в топик правила (если включён) или в общий поток.

        Никогда не поднимает исключение наверх: уведомление вторично по
        отношению к ответу/сохранению лида.
        """
        try:
            settings = await self._load_settings(db)
            if settings is None:
                return
            token, group_id = settings
            if notify_topic_enabled and rule_id is not None:
                thread_id = await self._ensure_topic(db, token, group_id, rule_id, rule_name)
            else:
                thread_id = await self._ensure_stream_topic(db, token, group_id)
            await self._call(
                token,
                "sendMessage",
                chat_id=group_id,
                message_thread_id=thread_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as exc:  # noqa: BLE001 — уведомление не критично
            logger.warning("notify_failed", detail=str(exc)[:200])
            await self._record_error(db, str(exc)[:300])

    # --- карточки совпадений (MatchLogReporter) -------------------------------
    async def log_target(self, db: AsyncSession, *, rule_id: uuid.UUID | None) -> LogTarget | None:
        """Куда слать карточку совпадения правила; None — уведомления выключены.

        Та же маршрутизация, что у notify_lead: свой топик правила, если он у
        правила включён, иначе общий поток «Общение ИИ». Режим «каждое
        совпадение» (log_all_matches) адрес не меняет — он в target.all_matches:
        выключен — карточка уходит, только если ответ отправлен (как было до
        него). Для своего топика правила заодно отдаём общий поток: туда
        уходит короткая строка об отправленном ответе со ссылкой на карточку.
        """
        row = await db.get(NotifySettings, SINGLETON_ID)
        if row is None:
            return None
        loaded = await self._load_settings(db)
        if loaded is None:
            return None
        token, group_id = loaded
        rule = await db.get(Rule, rule_id) if rule_id is not None else None
        stream_thread_id: int | None = None
        if rule is not None and rule.notify_topic_enabled:
            thread_id = await self._ensure_topic(db, token, group_id, rule.id, rule.name)
            stream_thread_id = await self._ensure_stream_topic(db, token, group_id)
        else:
            thread_id = await self._ensure_stream_topic(db, token, group_id)
        return LogTarget(
            token=token,
            group_id=group_id,
            thread_id=thread_id,
            all_matches=row.log_all_matches is not False,
            stream_thread_id=stream_thread_id,
        )

    async def log_all_matches_enabled(self, db: AsyncSession) -> bool:
        """Включён ли режим «карточка на каждое совпадение» (по умолчанию — да).

        При нём отдельную карточку лида (SAVE_LEAD) не шлём: лид виден в
        карточке совпадения, иначе на одно сообщение приходило бы два.
        """
        row = await db.get(NotifySettings, SINGLETON_ID)
        return row is None or row.log_all_matches is not False

    async def send_log_card(
        self, target: LogTarget, text: str, *, reply_to: int | None = None
    ) -> int:
        """Шлёт карточку (или ответ на неё) и возвращает id сообщения в группе.

        Поднимает NotifyRateLimitError/NotifyError: повтор и учёт лимитов — на
        стороне MatchLogReporter (место в лимите он уже занял — counted).
        """
        params: dict[str, Any] = {
            "chat_id": target.group_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if target.thread_id is not None:
            params["message_thread_id"] = target.thread_id
        if reply_to is not None:
            params["reply_to_message_id"] = reply_to
            params["allow_sending_without_reply"] = True
        result = await self._call(target.token, "sendMessage", counted=True, **params)
        return int(result["message_id"])

    async def edit_log_card(self, target: LogTarget, message_id: int, text: str) -> None:
        """Переписывает карточку на месте. «Не изменилось» — не ошибка."""
        try:
            await self._call(
                target.token,
                "editMessageText",
                counted=True,
                chat_id=target.group_id,
                message_id=message_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except NotifyRateLimitError:
            raise
        except NotifyError as exc:
            if "message is not modified" in str(exc):
                return
            raise

    async def note_on_card(
        self,
        token: str,
        group_id: int,
        thread_id: int | None,
        message_id: int,
        text: str,
    ) -> None:
        """Короткая строка ответом на карточку в её топике (решение по ревью).

        Никогда не поднимает исключение: это пометка, а не работа.
        """
        params: dict[str, Any] = {
            "chat_id": group_id,
            "text": text,
            "reply_to_message_id": message_id,
            "allow_sending_without_reply": True,
        }
        if thread_id is not None:
            params["message_thread_id"] = thread_id
        try:
            await self._call(token, "sendMessage", **params)
        except Exception as exc:  # noqa: BLE001 — пометка не критична
            logger.warning("note_on_card_failed", detail=str(exc)[:200])

    async def record_error(self, db: AsyncSession, detail: str) -> None:
        """Последняя ошибка бота — видна в настройках панели."""
        await self._record_error(db, detail)

    async def send_review(self, db: AsyncSession, review: Any) -> None:
        """Шлёт карточку сомнительного ответа с кнопками в топик «на подтверждение».

        review — строка PendingReview. Топик создаётся лениво. id отправленного
        сообщения запоминаем в review.notify_message_id, чтобы потом отредактировать
        карточку после решения оператора. Никогда не роняет обработку.

        На 429 — одна повторная попытка после retry_after (если ждать недолго,
        см. REVIEW_RETRY_MAX_SECONDS): без карточки оператор не увидит кнопок.
        """
        try:
            settings = await self._load_settings(db)
            if settings is None:
                return
            token, group_id = settings
            thread_id = await self._ensure_stream_topic(db, token, group_id)
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "✅ Отправить", "callback_data": f"rv_send:{review.id}"},
                        {"text": "✖️ Проигнорировать", "callback_data": f"rv_skip:{review.id}"},
                    ]
                ]
            }
            chat = await db.get(Chat, review.chat_id) if review.chat_id else None
            params: dict[str, Any] = {
                "chat_id": group_id,
                "message_thread_id": thread_id,
                "text": format_review_card(
                    review,
                    chat_title=chat.title if chat else None,
                    chat_username=chat.username if chat else None,
                ),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": keyboard,
            }
            try:
                result = await self._call(token, "sendMessage", **params)
            except NotifyRateLimitError as exc:
                if exc.retry_after > REVIEW_RETRY_MAX_SECONDS:
                    raise
                logger.warning("send_review_throttled", retry_after=exc.retry_after)
                await self._sleep(exc.retry_after)
                result = await self._call(token, "sendMessage", **params)
            review.notify_message_id = int(result["message_id"])
            await db.flush()
        except Exception as exc:  # noqa: BLE001 — уведомление не критично
            logger.warning("send_review_failed", detail=str(exc)[:200])
            await self._record_error(db, f"карточка ревью: {exc}"[:300])

    async def notify_digest(self, db: AsyncSession, text: str) -> None:
        """Шлёт дневную сводку в топик «Дайджест» (создаёт лениво)."""
        try:
            settings = await self._load_settings(db)
            if settings is None:
                return
            token, group_id = settings
            row = await db.get(NotifySettings, SINGLETON_ID)
            if row is None:
                return
            thread_id = row.digest_topic_id
            if thread_id is None:
                topic = await self._call(
                    token, "createForumTopic", chat_id=group_id, name="Дайджест"
                )
                thread_id = int(topic["message_thread_id"])
                row.digest_topic_id = thread_id
                await db.flush()
            await self._call(
                token,
                "sendMessage",
                chat_id=group_id,
                message_thread_id=thread_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as exc:  # noqa: BLE001 — дайджест не критичнее работы
            logger.warning("notify_digest_failed", detail=str(exc)[:200])

    async def load_token(self, db: AsyncSession) -> tuple[str, int] | None:
        """Токен и id группы для опроса нажатий (или None, если не настроено)."""
        return await self._load_settings(db, require_enabled=True)

    async def poll_updates(self, token: str, offset: int) -> list[dict[str, Any]]:
        """Забирает обновления бота (нажатия кнопок и /start) через long-poll."""
        resp = await self._client.post(
            f"{API_BASE}/bot{token}/getUpdates",
            json={
                "offset": offset,
                "timeout": 25,
                "allowed_updates": ["callback_query", "message"],
            },
            timeout=httpx.Timeout(35.0),
        )
        data = resp.json()
        if not data.get("ok"):
            raise NotifyError(data.get("description") or "getUpdates failed")
        result = data["result"]
        return list(result) if isinstance(result, list) else []

    async def answer_callback(self, token: str, callback_id: str, text: str) -> None:
        with contextlib.suppress(Exception):
            await self._call(
                token, "answerCallbackQuery", callback_query_id=callback_id, text=text
            )

    async def send_start_menu(self, token: str, chat_id: int) -> None:
        """Ответ на /start: диагностика ИИ + статистика авто-просмотра историй."""
        with contextlib.suppress(Exception):
            await self._call(
                token,
                "sendMessage",
                chat_id=chat_id,
                text="Бот-уведомитель Lipton Lead Gen.",
                reply_markup=_START_MENU_KEYBOARD,
            )

    async def edit_message(self, token: str, chat_id: int, message_id: int, text: str) -> None:
        """Обновляет текст сообщения на месте (используется карточкой проверки ИИ)."""
        with contextlib.suppress(Exception):
            await self._call(
                token,
                "editMessageText",
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=_CHECK_AI_KEYBOARD,
            )

    async def finalize_review_card(
        self, token: str, group_id: int, message_id: int, suffix: str
    ) -> None:
        """Убирает кнопки и дописывает исход после решения оператора."""
        with contextlib.suppress(Exception):
            await self._call(
                token,
                "editMessageReplyMarkup",
                chat_id=group_id,
                message_id=message_id,
                reply_markup={"inline_keyboard": []},
            )
        with contextlib.suppress(Exception):
            await self._call(
                token,
                "sendMessage",
                chat_id=group_id,
                reply_to_message_id=message_id,
                text=suffix,
            )

    async def _load_settings(
        self, db: AsyncSession, *, require_enabled: bool = True
    ) -> tuple[str, int] | None:
        row = await db.get(NotifySettings, SINGLETON_ID)
        if row is None or row.group_id is None:
            return None
        if require_enabled and not row.enabled:
            return None
        if not (row.bot_token_ct and row.bot_token_nonce and row.bot_token_key_id):
            return None
        token = self._box.decrypt_str(
            EncryptedBlob(
                row.bot_token_ct, row.bot_token_nonce, row.bot_token_key_id, "AES-256-GCM"
            ),
            aad="notify",
        )
        return token, row.group_id

    async def _ensure_topic(
        self,
        db: AsyncSession,
        token: str,
        group_id: int,
        rule_id: uuid.UUID | None,
        rule_name: str | None,
    ) -> int | None:
        """Возвращает id своего топика правила, создавая его при первом обращении."""
        if rule_id is None:
            return None

        rule = await db.get(Rule, rule_id)
        if rule is not None and rule.notify_topic_id is not None:
            return rule.notify_topic_id

        topic = await self._call(
            token,
            "createForumTopic",
            chat_id=group_id,
            name=(rule_name or "Лиды")[:128],
        )
        thread_id = int(topic["message_thread_id"])
        await db.execute(
            update(Rule).where(Rule.id == rule_id).values(notify_topic_id=thread_id)
        )
        return thread_id

    async def _record_error(self, db: AsyncSession, detail: str) -> None:
        with contextlib.suppress(Exception):
            await db.execute(
                update(NotifySettings)
                .where(NotifySettings.id == SINGLETON_ID)
                .values(last_error=detail)
            )


class NotifyError(RuntimeError):
    """Ошибка Bot API."""


class NotifyRateLimitError(NotifyError):
    """429 Too Many Requests: Telegram просит подождать retry_after секунд."""

    def __init__(self, message: str, *, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class NotifyTransientError(NotifyError):
    """Временный сбой Bot API (5xx, ответ не JSON): повтор имеет смысл."""


#: Описания Bot API, после которых карточку уже не отредактировать: её удалили,
#: она слишком старая или id неверный. Остальные ошибки правки — не повод
#: навсегда переходить на ответы.
_UNEDITABLE_MARKERS = (
    "message to edit not found",
    "message can't be edited",
    "message_id_invalid",
    "message identifier is not specified",
)


def is_uneditable(exc: BaseException) -> bool:
    """Ошибка правки значит «это сообщение больше не отредактировать»."""
    if not isinstance(exc, NotifyError) or isinstance(exc, NotifyTransientError):
        return False
    text = str(exc).lower()
    return any(marker in text for marker in _UNEDITABLE_MARKERS)


def is_transient(exc: BaseException) -> bool:
    """Сбой, который стоит повторить позже: 429, 5xx, сеть, битый ответ."""
    return isinstance(exc, NotifyRateLimitError | NotifyTransientError | httpx.TransportError)


@dataclass(frozen=True, slots=True)
class LogTarget:
    """Куда шлётся карточка: группа и топик. Токен не попадает в repr/логи."""

    token: str = field(repr=False)
    group_id: int
    thread_id: int | None
    # Режим «каждое совпадение». Выключен — карточка уходит, только если
    # ответ отправлен (как было до этого режима).
    all_matches: bool = True
    # У правила свой топик: сюда (общий поток «Общение ИИ») уходит короткая
    # строка об отправленном ответе со ссылкой на карточку. None — не нужно.
    stream_thread_id: int | None = None


def format_lead_card(
    *,
    rule_name: str | None,
    account_label: str,
    chat_title: str | None,
    sender_name: str | None,
    sender_username: str | None,
    sender_tg_id: int | None,
    incoming_text: str,
    reply_text: str = "",
    score: int,
    status: str,
    is_private: bool = False,
    chat_username: str | None = None,
    tg_chat_id: int | None = None,
    tg_message_id: int | None = None,
) -> str:
    """Карточка лида для топика: кто, откуда, текст, наш ответ (если был), ссылка."""
    who = sender_name or (f"@{sender_username}" if sender_username else str(sender_tg_id or "?"))
    link = (
        f"@{sender_username}"
        if sender_username
        else (f'<a href="tg://user?id={sender_tg_id}">написать</a>' if sender_tg_id else "—")
    )
    # Источник: личку зовём личкой; для группы — реальное имя/@username чата,
    # а не «личка» по умолчанию (иначе группы без названия ошибочно шли как личка).
    if is_private:
        where = "личка"
    else:
        where = chat_title or (f"@{chat_username}" if chat_username else "группа")
    where_line = f"💬 из: {_esc(where)}"
    msg_link = None if is_private else message_link(tg_chat_id, tg_message_id, chat_username)
    if msg_link:
        where_line += f' · <a href="{msg_link}">сообщение</a>'
    where_line += f" · аккаунт {_esc(account_label)}"
    parts = [
        f"🎯 <b>Лид</b> · {status} ({score})",
        f"👤 {_esc(who)} · {link}",
        where_line,
        f"\n<b>Сообщение:</b>\n{_esc(incoming_text[:400])}",
    ]
    if reply_text.strip():
        parts.append(f"\n<b>Наш ответ:</b>\n{_esc(reply_text[:400])}")
    return "\n".join(parts)


def message_link(
    tg_chat_id: int | None, message_id: int | None, username: str | None
) -> str | None:
    """Ссылка на сообщение в Telegram, если её вообще можно построить.

    Публичный чат — по username; приватный супергруппа/канал (-100…) — через
    /c/. Для лички и обычных групп прямой ссылки на сообщение нет.
    """
    if not tg_chat_id or not message_id:
        return None
    if username:
        return f"https://t.me/{username}/{message_id}"
    cid = str(tg_chat_id)
    if cid.startswith("-100"):
        return f"https://t.me/c/{cid[4:]}/{message_id}"
    return None


def format_review_card(
    review: Any, *, chat_title: str | None = None, chat_username: str | None = None
) -> str:
    """Карточка сомнительного ответа: откуда, что пришло, что предлагаем ответить."""
    handle = f"@{review.sender_username}" if review.sender_username else None
    who = review.sender_display_name or handle or str(review.target_sender_tg_id or "?")
    conf = f"{float(review.confidence):.2f}" if review.confidence is not None else "?"
    where = chat_title or (f"@{chat_username}" if chat_username else "личка")
    link = message_link(review.tg_chat_id, review.reply_to_tg_message_id, chat_username)
    where_line = f"💬 из: {_esc(where)}"
    if link:
        where_line += f' · <a href="{link}">открыть сообщение</a>'
    parts = [
        f"🟡 <b>Сомнительный лид</b> · уверенность {conf}",
        f"👤 {_esc(who)}",
        where_line,
        f"\n<b>Сообщение:</b>\n{_esc((review.incoming_text or '')[:400])}",
        f"\n<b>Предлагаю ответить:</b>\n{_esc((review.dm_text or review.reply_text or '')[:500])}",
    ]
    return "\n".join(parts)


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
