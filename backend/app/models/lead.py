"""Лиды (ТЗ §23)."""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models._types import TelegramId, pg_enum
from app.models.enums import LeadStatus


class Lead(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "leads"

    account_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    tg_user_id: Mapped[int] = mapped_column(TelegramId, nullable=False)
    username: Mapped[str | None] = mapped_column(sa.String(64))
    display_name: Mapped[str | None] = mapped_column(sa.String(255))

    source_chat_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("chats.id", ondelete="SET NULL")
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("conversations.id", ondelete="SET NULL")
    )
    scenario_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("scenarios.id", ondelete="SET NULL")
    )

    intent: Mapped[str | None] = mapped_column(sa.String(64))
    # 0..100. Границы cold/warm/hot настраиваются, поэтому в базе только число.
    score: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    status: Mapped[LeadStatus] = mapped_column(
        pg_enum(LeadStatus, "lead_status"), nullable=False, default=LeadStatus.NEW
    )

    first_seen_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(sa.Text)

    __table_args__ = (
        sa.UniqueConstraint("account_id", "tg_user_id", name="uq_leads_account_id_tg_user_id"),
        sa.CheckConstraint("score >= 0 AND score <= 100", name="score_range"),
        sa.Index("ix_leads_status_score", "status", sa.desc("score")),
        sa.Index("ix_leads_created_at", sa.desc("created_at")),
    )
