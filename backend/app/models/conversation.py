"""Диалоги и память о собеседнике (ТЗ §14, §15, §22)."""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models._types import TelegramId, pg_enum
from app.models.enums import ConversationStatus


class Conversation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "conversations"

    account_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    peer_tg_id: Mapped[int] = mapped_column(TelegramId, nullable=False)
    chat_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("chats.id", ondelete="SET NULL")
    )
    scenario_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("scenarios.id", ondelete="SET NULL")
    )

    status: Mapped[ConversationStatus] = mapped_column(
        pg_enum(ConversationStatus, "conversation_status"),
        nullable=False,
        default=ConversationStatus.NEW,
    )

    # Пересказ старой части переписки. Обновляется асинхронно, поэтому рядом
    # хранится граница: до какого сообщения пересказ актуален.
    summary: Mapped[str | None] = mapped_column(sa.Text)
    summary_upto_message_id: Mapped[int | None] = mapped_column(TelegramId)
    summary_updated_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    # A/B заходов: какой вариант-захода отправлен этому собеседнику первым и
    # засчитан ли уже его ответ (чтобы не увеличить reply_count дважды).
    ab_variant_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    ab_reply_counted: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)

    message_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    # Сколько ответов AI подряд без реплики собеседника. Счётчик обнуляется его
    # сообщением и ограничивает петлю «сам себе отвечаю» (ТЗ §9).
    ai_replies_in_row: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    last_message_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_reply_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (
        sa.UniqueConstraint(
            "account_id", "peer_tg_id", name="uq_conversations_account_id_peer_tg_id"
        ),
        sa.Index("ix_conversations_status", "status"),
        sa.Index("ix_conversations_last_message_at", sa.desc("last_message_at")),
    )


class UserMemory(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Устойчивые факты о собеседнике.

    Не журнал переписки: сюда попадает только то, что имеет смысл помнить
    следующие недели — интерес, намерение, ограничения, статус сделки.
    Ключ уникален, поэтому новый факт перезаписывает старый, а не копится.
    """

    __tablename__ = "user_memory"

    account_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    peer_tg_id: Mapped[int] = mapped_column(TelegramId, nullable=False)

    key: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    value: Mapped[str] = mapped_column(sa.Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column(sa.Numeric(3, 2))
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("messages.id", ondelete="SET NULL")
    )

    __table_args__ = (
        sa.UniqueConstraint(
            "account_id", "peer_tg_id", "key", name="uq_user_memory_account_id_peer_tg_id_key"
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="confidence_range",
        ),
    )
