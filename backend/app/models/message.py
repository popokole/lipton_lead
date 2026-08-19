"""Сообщения и защита от повторной обработки.

Две таблицы вместо одной — сознательно. В `messages` попадает только то, что
имеет ценность: сообщения отслеживаемых чатов, прошедшие базовые фильтры, и
вся переписка в личных диалогах. Если писать туда каждое сообщение крупного
чата, таблица вырастет на миллионы строк в неделю, из которых 99% никогда не
понадобятся.

`processed_messages` — лёгкий журнал «застолблено»: три числа и время. Строка
вставляется ДО обработки, поэтому повторная доставка события от Telethon
(реконнект, catch-up) не приводит ко второму походу в AI.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, UUIDPrimaryKeyMixin
from app.models._types import TelegramId, pg_enum
from app.models.enums import MediaType, ProcessedStatus


class Message(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "messages"

    account_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    chat_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("chats.id", ondelete="SET NULL")
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("conversations.id", ondelete="SET NULL")
    )

    tg_chat_id: Mapped[int] = mapped_column(TelegramId, nullable=False)
    tg_message_id: Mapped[int] = mapped_column(TelegramId, nullable=False)
    sender_tg_id: Mapped[int | None] = mapped_column(TelegramId)
    sender_username: Mapped[str | None] = mapped_column(sa.String(64))
    sender_display_name: Mapped[str | None] = mapped_column(sa.String(255))

    text: Mapped[str | None] = mapped_column(sa.Text)
    date: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    reply_to_tg_message_id: Mapped[int | None] = mapped_column(TelegramId)

    is_incoming: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    is_outgoing: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    is_forwarded: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    # Ответ, отправленный самой платформой. Такие сообщения никогда не
    # возвращаются в конвейер — иначе аккаунт начнёт отвечать сам себе.
    is_bot_reply: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)

    media_type: Mapped[MediaType | None] = mapped_column(pg_enum(MediaType, "media_type"))
    lang: Mapped[str | None] = mapped_column(sa.String(8))

    processed_status: Mapped[ProcessedStatus] = mapped_column(
        pg_enum(ProcessedStatus, "processed_status"),
        nullable=False,
        default=ProcessedStatus.PENDING,
    )
    rule_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("rules.id", ondelete="SET NULL")
    )

    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (
        sa.UniqueConstraint(
            "account_id",
            "tg_chat_id",
            "tg_message_id",
            name="uq_messages_account_id_tg_chat_id_tg_message_id",
        ),
        sa.CheckConstraint("is_incoming <> is_outgoing", name="direction_exclusive"),
        sa.Index("ix_messages_chat_id_date", "chat_id", sa.desc("date")),
        sa.Index("ix_messages_account_id_date", "account_id", sa.desc("date")),
        sa.Index("ix_messages_sender_tg_id", "sender_tg_id"),
        sa.Index("ix_messages_conversation_id_date", "conversation_id", "date"),
        sa.Index("ix_messages_processed_status", "processed_status"),
        sa.Index("ix_messages_created_at", "created_at"),
    )


class ProcessedMessage(Base):
    """Отметка «сообщение уже взято в обработку».

    Ключ совпадает с ключом дедупликации из ТЗ §7. Запись живёт недолго —
    ретеншен чистит её через DUPLICATE_WINDOW_SECONDS.
    """

    __tablename__ = "processed_messages"

    account_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        sa.ForeignKey("accounts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tg_chat_id: Mapped[int] = mapped_column(TelegramId, primary_key=True)
    tg_message_id: Mapped[int] = mapped_column(TelegramId, primary_key=True)

    claimed_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    stage: Mapped[str | None] = mapped_column(sa.String(32))

    __table_args__ = (sa.Index("ix_processed_messages_claimed_at", "claimed_at"),)
