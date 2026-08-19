"""Запросы по чатам."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utcnow
from app.models import Chat, ChatType


class ChatRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get(self, account_id: uuid.UUID, tg_chat_id: int) -> Chat | None:
        return await self._db.scalar(
            select(Chat).where(Chat.account_id == account_id, Chat.tg_chat_id == tg_chat_id)
        )

    async def list_monitored(self, account_id: uuid.UUID) -> list[Chat]:
        rows = await self._db.scalars(
            select(Chat).where(Chat.account_id == account_id, Chat.monitored.is_(True))
        )
        return list(rows.all())

    async def ensure(
        self,
        account_id: uuid.UUID,
        tg_chat_id: int,
        *,
        chat_type: ChatType,
        title: str | None = None,
        username: str | None = None,
        monitored: bool = False,
    ) -> Chat:
        """Находит чат или создаёт его.

        Личные диалоги появляются сами по факту переписки — заводить их в
        панели вручную не нужно. Групповые чаты создаются с monitored=False:
        решение следить за чатом принимает оператор, а не входящее сообщение.
        """
        chat = await self.get(account_id, tg_chat_id)
        if chat is not None:
            if title and chat.title != title:
                chat.title = title
            if username and chat.username != username:
                chat.username = username
            return chat

        # Два сообщения из одного чата приходят почти одновременно и оба
        # пытаются создать чат. ON CONFLICT DO NOTHING делает вставку
        # безопасной: проигравший просто перечитает уже созданную строку.
        stmt = (
            pg_insert(Chat)
            .values(
                account_id=account_id,
                tg_chat_id=tg_chat_id,
                type=chat_type,
                title=title,
                username=username,
                monitored=monitored,
            )
            .on_conflict_do_nothing(index_elements=["account_id", "tg_chat_id"])
        )
        await self._db.execute(stmt)
        await self._db.flush()

        created = await self.get(account_id, tg_chat_id)
        assert created is not None
        return created

    async def touch(self, chat_id: uuid.UUID) -> None:
        chat = await self._db.get(Chat, chat_id)
        if chat is not None:
            chat.last_message_at = utcnow()

    async def set_monitored(self, chat_id: uuid.UUID, monitored: bool) -> None:
        chat = await self._db.get(Chat, chat_id)
        if chat is not None:
            chat.monitored = monitored

    async def set_avatar(self, chat_id: uuid.UUID, data: bytes | None, mime: str | None) -> None:
        chat = await self._db.get(Chat, chat_id)
        if chat is None:
            return
        chat.avatar = data
        chat.avatar_mime = mime
        chat.avatar_fetched_at = utcnow()
