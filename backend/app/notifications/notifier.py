"""Бот-уведомления: отчёты о лидах в форум-группу (ТЗ §36).

Отдельный бот (токен @BotFather) шлёт карточку лида в топик, свой на каждый
сценарий. Топик создаётся лениво один раз и запоминается в `scenario.notify_topic_id`.

Это Bot API (api.telegram.org), а не пользовательский клиент Telethon —
поэтому обычный httpx, без сессий и аренды. Работает в воркере рядом с
отправкой ответа, но полностью изолирован: любой сбой бота не должен влиять
на сам ответ лиду.
"""

from __future__ import annotations

import contextlib
import uuid
from typing import Any

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import EncryptedBlob, SecretBox
from app.core.logging import get_logger
from app.models import Chat, Scenario
from app.models.notify import SINGLETON_ID, NotifySettings

logger = get_logger(__name__)

API_BASE = "https://api.telegram.org"


class NotifierBot:
    """Отправка карточек лидов через Bot API форум-группы."""

    def __init__(self, box: SecretBox, proxy: str | None = None) -> None:
        self._box = box
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(20.0), proxy=proxy)

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(self, token: str, method: str, **params: Any) -> dict[str, Any]:
        resp = await self._client.post(f"{API_BASE}/bot{token}/{method}", json=params)
        data = resp.json()
        if not data.get("ok"):
            raise NotifyError(data.get("description") or f"Bot API {method} failed")
        return data["result"]

    async def check(self, token: str) -> str:
        """Проверяет токен, возвращает username бота."""
        me = await self._call(token, "getMe")
        return str(me.get("username") or "")

    async def sync_topics(self, db: AsyncSession, *, rename: bool) -> dict[str, Any]:
        """Готовит группу: топик на каждый сценарий.

        Проверяет, что группа — форум (иначе топики создать нельзя), затем на
        каждый сценарий создаёт топик, если его ещё нет. При rename=True
        приводит имя топика к текущему имени сценария (editForumTopic).
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

        scenarios = list((await db.scalars(select(Scenario))).all())
        created = existing = renamed = 0
        for scenario in scenarios:
            if scenario.notify_topic_id is None:
                topic = await self._call(
                    token, "createForumTopic", chat_id=group_id, name=scenario.name[:128]
                )
                scenario.notify_topic_id = int(topic["message_thread_id"])
                created += 1
            else:
                existing += 1
                if rename:
                    try:
                        await self._call(
                            token,
                            "editForumTopic",
                            chat_id=group_id,
                            message_thread_id=scenario.notify_topic_id,
                            name=scenario.name[:128],
                        )
                        renamed += 1
                    except NotifyError:
                        pass  # топик мог быть удалён вручную — не критично

        # Два постоянных топика-ленты: все ответы в личке и все в группах.
        row = await db.get(NotifySettings, SINGLETON_ID)
        if row is not None:
            if row.dm_topic_id is None:
                topic = await self._call(
                    token, "createForumTopic", chat_id=group_id, name="Общение ИИ · личка"
                )
                row.dm_topic_id = int(topic["message_thread_id"])
                created += 1
            if row.group_topic_id is None:
                topic = await self._call(
                    token, "createForumTopic", chat_id=group_id, name="Общение ИИ · группы"
                )
                row.group_topic_id = int(topic["message_thread_id"])
                created += 1
            if row.review_topic_id is None:
                topic = await self._call(
                    token, "createForumTopic", chat_id=group_id, name="На подтверждение"
                )
                row.review_topic_id = int(topic["message_thread_id"])
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
            "scenarios": len(scenarios),
            "created": created,
            "existing": existing,
            "renamed": renamed,
        }

    async def notify_stream(self, db: AsyncSession, *, is_private: bool, text: str) -> None:
        """Шлёт ответ в постоянный топик-ленту: личка или группы.

        Отдельно от notify_lead (топик сценария): здесь копятся ВСЕ наши
        ответы двумя лентами. Топик создаётся лениво при первом обращении.
        Никогда не поднимает исключение — уведомление вторично.
        """
        try:
            settings = await self._load_settings(db)
            if settings is None:
                return
            token, group_id = settings
            row = await db.get(NotifySettings, SINGLETON_ID)
            if row is None:
                return
            field = "dm_topic_id" if is_private else "group_topic_id"
            thread_id = getattr(row, field)
            if thread_id is None:
                name = "Общение ИИ · личка" if is_private else "Общение ИИ · группы"
                topic = await self._call(
                    token, "createForumTopic", chat_id=group_id, name=name
                )
                thread_id = int(topic["message_thread_id"])
                setattr(row, field, thread_id)
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
        except Exception as exc:  # noqa: BLE001 — уведомление не критично
            logger.warning("notify_stream_failed", detail=str(exc)[:200])
            await self._record_error(db, str(exc)[:300])

    async def notify_lead(
        self,
        db: AsyncSession,
        *,
        scenario_id: uuid.UUID | None,
        scenario_name: str | None,
        text: str,
    ) -> None:
        """Шлёт готовый текст в топик сценария. Молча выходит, если выключено.

        Никогда не поднимает исключение наверх: уведомление вторично по
        отношению к ответу лиду.
        """
        try:
            settings = await self._load_settings(db)
            if settings is None:
                return
            token, group_id = settings
            thread_id = await self._ensure_topic(db, token, group_id, scenario_id, scenario_name)
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

    async def send_review(self, db: AsyncSession, review: Any) -> None:
        """Шлёт карточку сомнительного ответа с кнопками в топик «на подтверждение».

        review — строка PendingReview. Топик создаётся лениво. id отправленного
        сообщения запоминаем в review.notify_message_id, чтобы потом отредактировать
        карточку после решения оператора. Никогда не роняет обработку.
        """
        try:
            settings = await self._load_settings(db)
            if settings is None:
                return
            token, group_id = settings
            row = await db.get(NotifySettings, SINGLETON_ID)
            if row is None:
                return
            thread_id = row.review_topic_id
            if thread_id is None:
                topic = await self._call(
                    token, "createForumTopic", chat_id=group_id, name="На подтверждение"
                )
                thread_id = int(topic["message_thread_id"])
                row.review_topic_id = thread_id
                await db.flush()
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "✅ Отправить", "callback_data": f"rv_send:{review.id}"},
                        {"text": "✖️ Проигнорировать", "callback_data": f"rv_skip:{review.id}"},
                    ]
                ]
            }
            chat = await db.get(Chat, review.chat_id) if review.chat_id else None
            result = await self._call(
                token,
                "sendMessage",
                chat_id=group_id,
                message_thread_id=thread_id,
                text=format_review_card(
                    review,
                    chat_title=chat.title if chat else None,
                    chat_username=chat.username if chat else None,
                ),
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=keyboard,
            )
            review.notify_message_id = int(result["message_id"])
            await db.flush()
        except Exception as exc:  # noqa: BLE001 — уведомление не критично
            logger.warning("send_review_failed", detail=str(exc)[:200])

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
        """Забирает обновления бота (только нажатия кнопок) через long-poll."""
        resp = await self._client.post(
            f"{API_BASE}/bot{token}/getUpdates",
            json={
                "offset": offset,
                "timeout": 25,
                "allowed_updates": ["callback_query"],
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
        scenario_id: uuid.UUID | None,
        scenario_name: str | None,
    ) -> int | None:
        """Возвращает id топика сценария, создавая его при первом обращении.

        Без сценария (правило без сценария) шлём в общий поток группы: None.
        """
        if scenario_id is None:
            return None

        scenario = await db.get(Scenario, scenario_id)
        if scenario is not None and scenario.notify_topic_id is not None:
            return scenario.notify_topic_id

        topic = await self._call(
            token,
            "createForumTopic",
            chat_id=group_id,
            name=(scenario_name or "Лиды")[:128],
        )
        thread_id = int(topic["message_thread_id"])
        if scenario_id is not None:
            await db.execute(
                update(Scenario)
                .where(Scenario.id == scenario_id)
                .values(notify_topic_id=thread_id)
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


def format_lead_card(
    *,
    scenario_name: str | None,
    account_label: str,
    chat_title: str | None,
    sender_name: str | None,
    sender_username: str | None,
    sender_tg_id: int | None,
    incoming_text: str,
    reply_text: str,
    score: int,
    status: str,
) -> str:
    """Карточка лида для топика: кто, откуда, текст, наш ответ, ссылка."""
    who = sender_name or (f"@{sender_username}" if sender_username else str(sender_tg_id or "?"))
    link = (
        f"@{sender_username}"
        if sender_username
        else (f'<a href="tg://user?id={sender_tg_id}">написать</a>' if sender_tg_id else "—")
    )
    where = chat_title or "личка"
    return (
        f"🎯 <b>Лид</b> · {status} ({score})\n"
        f"👤 {_esc(who)} · {link}\n"
        f"💬 из: {_esc(where)} · аккаунт {_esc(account_label)}\n\n"
        f"<b>Сообщение:</b>\n{_esc(incoming_text[:400])}\n\n"
        f"<b>Наш ответ:</b>\n{_esc(reply_text[:400])}"
    )


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
