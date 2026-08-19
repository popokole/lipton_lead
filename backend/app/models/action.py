"""Действия и журнал их выполнения (ТЗ §18)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models._types import TelegramId, pg_enum
from app.models.enums import ActionStatus, ActionType


class Action(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Намерение что-то сделать: ответить, уведомить, передать человеку.

    `dedup_key` делает отправку идемпотентной: повторная попытка после сбоя
    сети не приведёт ко второму сообщению в чате.
    """

    __tablename__ = "actions"

    type: Mapped[ActionType] = mapped_column(pg_enum(ActionType, "action_type"), nullable=False)
    status: Mapped[ActionStatus] = mapped_column(
        pg_enum(ActionStatus, "action_status"), nullable=False, default=ActionStatus.PENDING
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    chat_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("chats.id", ondelete="SET NULL")
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("messages.id", ondelete="SET NULL")
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("conversations.id", ondelete="SET NULL")
    )
    rule_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("rules.id", ondelete="SET NULL")
    )
    scenario_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("scenarios.id", ondelete="SET NULL")
    )

    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb")
    )
    reply_text: Mapped[str | None] = mapped_column(sa.Text)
    # Итог работы валидатора: какие проверки прошли, какая завалила отправку.
    validation: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    dedup_key: Mapped[str] = mapped_column(sa.String(200), nullable=False, unique=True)
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    scheduled_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    sent_tg_message_id: Mapped[int | None] = mapped_column(TelegramId)
    error: Mapped[str | None] = mapped_column(sa.Text)

    __table_args__ = (
        sa.Index("ix_actions_status_scheduled_at", "status", "scheduled_at"),
        sa.Index("ix_actions_account_id_created_at", "account_id", sa.desc("created_at")),
        sa.Index("ix_actions_conversation_id", "conversation_id"),
    )


class ActionLog(Base):
    """Пошаговый след выполнения действия."""

    __tablename__ = "action_logs"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    action_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("actions.id", ondelete="CASCADE"), nullable=False
    )
    at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    stage: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (sa.Index("ix_action_logs_action_id_at", "action_id", "at"),)
