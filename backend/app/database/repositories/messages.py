"""Запросы по сообщениям и защита от повторной обработки.

Ключевой метод здесь — `claim`. Он выполняет вставку с ON CONFLICT DO NOTHING
и возвращает, удалось ли застолбить сообщение. Такая проверка атомарна на
уровне базы, поэтому две задачи, получившие одно и то же событие от Telethon,
не смогут обе пройти дальше — а именно это защищает от второго ответа в чат.

Проверка «а нет ли уже такого сообщения?» отдельным SELECT здесь не годится
принципиально: между SELECT и INSERT успевает вклиниться второй обработчик.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete, desc, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utcnow
from app.database.base import affected_rows
from app.models import Message, ProcessedMessage, ProcessedStatus
from app.telegram.messages import NormalizedMessage


class MessageRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def claim(self, message: NormalizedMessage, stage: str = "received") -> bool:
        """Пытается застолбить сообщение. False — его уже кто-то взял."""
        account_id, tg_chat_id, tg_message_id = message.dedup_key
        statement = (
            pg_insert(ProcessedMessage)
            .values(
                account_id=account_id,
                tg_chat_id=tg_chat_id,
                tg_message_id=tg_message_id,
                claimed_at=utcnow(),
                stage=stage,
            )
            .on_conflict_do_nothing(index_elements=["account_id", "tg_chat_id", "tg_message_id"])
        )
        result = await self._db.execute(statement)
        return affected_rows(result) == 1

    async def release_claim(self, message: NormalizedMessage) -> None:
        """Снимает отметку, если обработка так и не началась.

        Нужно ровно в одном случае: конвейер упал до сохранения сообщения, и
        повторная доставка того же события должна получить ещё один шанс.
        """
        account_id, tg_chat_id, tg_message_id = message.dedup_key
        await self._db.execute(
            delete(ProcessedMessage).where(
                ProcessedMessage.account_id == account_id,
                ProcessedMessage.tg_chat_id == tg_chat_id,
                ProcessedMessage.tg_message_id == tg_message_id,
            )
        )

    async def save(
        self,
        message: NormalizedMessage,
        *,
        chat_id: uuid.UUID | None,
        conversation_id: uuid.UUID | None = None,
        status: ProcessedStatus = ProcessedStatus.PENDING,
        is_bot_reply: bool = False,
    ) -> Message:
        row = Message(
            account_id=message.account_id,
            chat_id=chat_id,
            conversation_id=conversation_id,
            tg_chat_id=message.tg_chat_id,
            tg_message_id=message.tg_message_id,
            sender_tg_id=message.sender_tg_id,
            sender_username=message.sender_username,
            sender_display_name=message.sender_display_name,
            text=message.text or None,
            date=message.date,
            reply_to_tg_message_id=message.reply_to_tg_message_id,
            is_incoming=message.is_incoming,
            is_outgoing=message.is_outgoing,
            is_forwarded=message.is_forwarded,
            is_bot_reply=is_bot_reply,
            media_type=message.media_type,
            processed_status=status,
        )
        self._db.add(row)
        await self._db.flush()
        return row

    async def set_status(
        self,
        message_id: uuid.UUID,
        status: ProcessedStatus,
        *,
        rule_id: uuid.UUID | None = None,
        reason: str | None = None,
    ) -> None:
        row = await self._db.get(Message, message_id)
        if row is None:
            return
        row.processed_status = status
        if rule_id is not None:
            row.rule_id = rule_id
        if reason is not None:
            row.status_reason = reason
        await self._db.flush()

    async def recent_in_chat(
        self, account_id: uuid.UUID, tg_chat_id: int, limit: int
    ) -> list[Message]:
        """Последние сообщения чата — от свежих к старым."""
        rows = await self._db.scalars(
            select(Message)
            .where(Message.account_id == account_id, Message.tg_chat_id == tg_chat_id)
            .order_by(desc(Message.date), desc(Message.tg_message_id))
            .limit(limit)
        )
        return list(rows.all())

    async def exists(self, message: NormalizedMessage) -> bool:
        account_id, tg_chat_id, tg_message_id = message.dedup_key
        found = await self._db.scalar(
            select(Message.id).where(
                Message.account_id == account_id,
                Message.tg_chat_id == tg_chat_id,
                Message.tg_message_id == tg_message_id,
            )
        )
        return found is not None

    async def purge_claims_before(self, cutoff: datetime) -> int:
        """Чистит журнал «застолблено»: он нужен только внутри окна дедупликации."""
        result = await self._db.execute(
            delete(ProcessedMessage).where(ProcessedMessage.claimed_at < cutoff)
        )
        return affected_rows(result)

    async def purge_claims_older_than(self, seconds: int) -> int:
        return await self.purge_claims_before(utcnow() - timedelta(seconds=seconds))
