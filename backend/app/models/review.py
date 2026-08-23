"""Ответы, ожидающие подтверждения оператора (human-in-the-loop).

Когда ИИ не уверен (пограничная уверенность), ответ не уходит собеседнику сразу:
он сохраняется здесь и отправляется в лог-чат карточкой с кнопками
«Отправить»/«Проигнорировать». Решение оператора обрабатывает воркер, который
и владеет клиентом Telegram для реальной отправки.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, UUIDPrimaryKeyMixin
from app.models._types import TelegramId


class PendingReview(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "pending_reviews"

    account_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    scenario_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("scenarios.id", ondelete="SET NULL")
    )
    rule_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("rules.id", ondelete="SET NULL")
    )
    chat_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("chats.id", ondelete="SET NULL")
    )

    # Куда уйдёт основной ответ (группа или личка) и в ответ на что.
    tg_chat_id: Mapped[int] = mapped_column(TelegramId, nullable=False)
    reply_to_tg_message_id: Mapped[int | None] = mapped_column(TelegramId)
    # Для режима «в чат + в личку»: кому уходит развёрнутый ответ в лс.
    target_sender_tg_id: Mapped[int | None] = mapped_column(TelegramId)

    reply_text: Mapped[str] = mapped_column(sa.Text, nullable=False)
    dm_text: Mapped[str | None] = mapped_column(sa.Text)
    incoming_text: Mapped[str | None] = mapped_column(sa.Text)
    sender_username: Mapped[str | None] = mapped_column(sa.String(64))
    sender_display_name: Mapped[str | None] = mapped_column(sa.String(255))
    confidence: Mapped[float | None] = mapped_column(sa.Numeric(3, 2))

    # pending → sent | ignored. Строкой, а не enum: значений мало и они стабильны.
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="pending")
    # id карточки в лог-чате, чтобы отредактировать её после решения.
    notify_message_id: Mapped[int | None] = mapped_column(TelegramId)

    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    decided_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (
        sa.Index("ix_pending_reviews_status", "status"),
        sa.Index("ix_pending_reviews_created_at", sa.desc("created_at")),
    )
