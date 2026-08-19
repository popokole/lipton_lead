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
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import EncryptedBlob, SecretBox
from app.core.logging import get_logger
from app.models import Scenario
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

    async def _load_settings(self, db: AsyncSession) -> tuple[str, int] | None:
        row = await db.get(NotifySettings, SINGLETON_ID)
        if row is None or not row.enabled or row.group_id is None:
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


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
