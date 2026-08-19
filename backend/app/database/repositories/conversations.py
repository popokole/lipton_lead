"""Диалоги, память о собеседнике и лиды."""

from __future__ import annotations

import uuid

from sqlalchemy import desc, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utcnow
from app.models import (
    Conversation,
    ConversationStatus,
    Lead,
    LeadStatus,
    Message,
    UserMemory,
)


class ConversationRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get_or_create(
        self,
        account_id: uuid.UUID,
        peer_tg_id: int,
        *,
        chat_id: uuid.UUID | None = None,
        scenario_id: uuid.UUID | None = None,
    ) -> Conversation:
        conversation = await self._db.scalar(
            select(Conversation).where(
                Conversation.account_id == account_id, Conversation.peer_tg_id == peer_tg_id
            )
        )
        if conversation is not None:
            return conversation

        conversation = Conversation(
            account_id=account_id,
            peer_tg_id=peer_tg_id,
            chat_id=chat_id,
            scenario_id=scenario_id,
            status=ConversationStatus.NEW,
        )
        self._db.add(conversation)
        await self._db.flush()
        return conversation

    async def register_incoming(self, conversation_id: uuid.UUID) -> None:
        """Сообщение собеседника: счётчик подряд идущих ответов обнуляется."""
        await self._db.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(
                message_count=Conversation.message_count + 1,
                ai_replies_in_row=0,
                last_message_at=utcnow(),
                status=ConversationStatus.ACTIVE,
            )
        )

    async def register_reply(self, conversation_id: uuid.UUID) -> None:
        """Наш ответ: счётчик растёт и ограничивает петлю самоответов."""
        await self._db.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(
                message_count=Conversation.message_count + 1,
                ai_replies_in_row=Conversation.ai_replies_in_row + 1,
                last_reply_at=utcnow(),
                last_message_at=utcnow(),
            )
        )

    async def set_status(self, conversation_id: uuid.UUID, status: ConversationStatus) -> None:
        await self._db.execute(
            update(Conversation).where(Conversation.id == conversation_id).values(status=status)
        )

    async def save_summary(
        self, conversation_id: uuid.UUID, summary: str, upto_message_id: int | None
    ) -> None:
        await self._db.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(
                summary=summary,
                summary_upto_message_id=upto_message_id,
                summary_updated_at=utcnow(),
            )
        )

    async def recent_bot_replies(
        self, account_id: uuid.UUID, tg_chat_id: int, limit: int = 5
    ) -> list[str]:
        """Последние наши ответы — для проверки на дубль."""
        rows = await self._db.scalars(
            select(Message.text)
            .where(
                Message.account_id == account_id,
                Message.tg_chat_id == tg_chat_id,
                Message.is_bot_reply.is_(True),
            )
            .order_by(desc(Message.date))
            .limit(limit)
        )
        return [text for text in rows.all() if text]


class UserMemoryRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def load(self, account_id: uuid.UUID, peer_tg_id: int) -> dict[str, str]:
        rows = await self._db.execute(
            select(UserMemory.key, UserMemory.value).where(
                UserMemory.account_id == account_id, UserMemory.peer_tg_id == peer_tg_id
            )
        )
        return {key: value for key, value in rows.all()}  # noqa: C416

    async def remember(
        self,
        account_id: uuid.UUID,
        peer_tg_id: int,
        *,
        key: str,
        value: str,
        confidence: float | None = None,
        source_message_id: uuid.UUID | None = None,
    ) -> None:
        """Новый факт замещает старый: память не должна расти повторами."""
        statement = (
            pg_insert(UserMemory)
            .values(
                account_id=account_id,
                peer_tg_id=peer_tg_id,
                key=key.strip()[:64],
                value=value.strip(),
                confidence=confidence,
                source_message_id=source_message_id,
            )
            .on_conflict_do_update(
                index_elements=["account_id", "peer_tg_id", "key"],
                set_={
                    "value": value.strip(),
                    "confidence": confidence,
                    "source_message_id": source_message_id,
                    "updated_at": utcnow(),
                },
            )
        )
        await self._db.execute(statement)

    async def forget_all(self, account_id: uuid.UUID, peer_tg_id: int) -> None:
        """Полная очистка памяти о собеседнике (ТЗ §15)."""
        from sqlalchemy import delete

        await self._db.execute(
            delete(UserMemory).where(
                UserMemory.account_id == account_id, UserMemory.peer_tg_id == peer_tg_id
            )
        )


class LeadRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def upsert(
        self,
        account_id: uuid.UUID,
        tg_user_id: int,
        *,
        score: int,
        intent: str | None = None,
        username: str | None = None,
        display_name: str | None = None,
        source_chat_id: uuid.UUID | None = None,
        conversation_id: uuid.UUID | None = None,
        scenario_id: uuid.UUID | None = None,
        cold_below: int = 30,
        hot_from: int = 70,
    ) -> Lead:
        """Создаёт или обновляет лида.

        Балл только растёт: интерес, проявленный однажды, не исчезает от
        последующего «спасибо, я подумаю».
        """
        lead = await self._db.scalar(
            select(Lead).where(Lead.account_id == account_id, Lead.tg_user_id == tg_user_id)
        )
        if lead is None:
            lead = Lead(
                account_id=account_id,
                tg_user_id=tg_user_id,
                username=username,
                display_name=display_name,
                source_chat_id=source_chat_id,
                conversation_id=conversation_id,
                scenario_id=scenario_id,
                score=0,
            )
            self._db.add(lead)

        lead.score = max(lead.score, min(max(score, 0), 100))
        lead.status = classify_lead(lead.score, cold_below=cold_below, hot_from=hot_from)
        lead.last_seen_at = utcnow()
        if intent:
            lead.intent = intent
        if username:
            lead.username = username
        if display_name:
            lead.display_name = display_name
        if conversation_id is not None:
            lead.conversation_id = conversation_id

        await self._db.flush()
        return lead


def classify_lead(score: int, *, cold_below: int = 30, hot_from: int = 70) -> LeadStatus:
    """Границы cold/warm/hot настраиваются (ТЗ §23)."""
    if score <= 0:
        return LeadStatus.NEW
    if score < cold_below:
        return LeadStatus.COLD
    if score < hot_from:
        return LeadStatus.WARM
    return LeadStatus.HOT
