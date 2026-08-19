"""Воркеры, события, расход AI, уведомления и аудит."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models._types import pg_enum
from app.models.enums import AIPurpose, EventType, NotificationType, WorkerStatus


class Worker(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Долговременная карточка воркера.

    Живой heartbeat лежит в Redis (запись раз в несколько секунд от каждого
    воркера не должна нагружать PostgreSQL). Здесь — история: когда воркер
    появился, что делал, на чём упал.
    """

    __tablename__ = "workers"

    name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    status: Mapped[WorkerStatus] = mapped_column(
        pg_enum(WorkerStatus, "worker_status"), nullable=False, default=WorkerStatus.STARTING
    )
    hostname: Mapped[str | None] = mapped_column(sa.String(255))
    pid: Mapped[int | None] = mapped_column(sa.Integer)
    version: Mapped[str | None] = mapped_column(sa.String(32))

    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    stopped_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    accounts_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    queue_size: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    last_processed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (
        sa.Index("ix_workers_status", "status"),
        sa.Index("ix_workers_heartbeat_at", sa.desc("heartbeat_at")),
    )


class EventLog(Base):
    """Структурированный журнал обработки (ТЗ §30).

    Пишется в базу, а не только в stdout, потому что панель обязана показывать
    путь конкретного сообщения. Секретов здесь не бывает: значения проходят
    через ту же редакцию, что и логи процесса.
    """

    __tablename__ = "event_logs"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    level: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="INFO")
    event_type: Mapped[EventType] = mapped_column(pg_enum(EventType, "event_type"), nullable=False)

    account_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("accounts.id", ondelete="CASCADE")
    )
    chat_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("chats.id", ondelete="SET NULL")
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("messages.id", ondelete="SET NULL")
    )
    scenario_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("scenarios.id", ondelete="SET NULL")
    )
    rule_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("rules.id", ondelete="SET NULL")
    )
    action_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("actions.id", ondelete="SET NULL")
    )
    worker_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("workers.id", ondelete="SET NULL")
    )

    duration_ms: Mapped[int | None] = mapped_column(sa.Integer)
    status: Mapped[str | None] = mapped_column(sa.String(32))
    error: Mapped[str | None] = mapped_column(sa.Text)
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        sa.Index("ix_event_logs_ts", sa.desc("ts")),
        sa.Index("ix_event_logs_event_type_ts", "event_type", sa.desc("ts")),
        sa.Index("ix_event_logs_account_id_ts", "account_id", sa.desc("ts")),
        sa.Index("ix_event_logs_message_id", "message_id"),
    )


class AIRequest(Base):
    """Каждое обращение к AI: токены, деньги, задержка.

    Без этой таблицы стоимость работы системы невидима, а дневной бюджет
    (AI_DAILY_BUDGET_USD) не на чем считать.
    """

    __tablename__ = "ai_requests"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    provider: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    model: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    purpose: Mapped[AIPurpose] = mapped_column(pg_enum(AIPurpose, "ai_purpose"), nullable=False)

    account_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("accounts.id", ondelete="SET NULL")
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("messages.id", ondelete="SET NULL")
    )
    scenario_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("scenarios.id", ondelete="SET NULL")
    )

    prompt_tokens: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(sa.Numeric(12, 6), nullable=False, default=0)
    latency_ms: Mapped[int | None] = mapped_column(sa.Integer)
    status: Mapped[str] = mapped_column(sa.String(32), nullable=False, default="OK")
    error: Mapped[str | None] = mapped_column(sa.Text)

    __table_args__ = (
        sa.Index("ix_ai_requests_created_at", sa.desc("created_at")),
        sa.Index("ix_ai_requests_purpose_created_at", "purpose", sa.desc("created_at")),
    )


class Notification(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "notifications"

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE")
    )
    type: Mapped[NotificationType] = mapped_column(
        pg_enum(NotificationType, "notification_type"), nullable=False
    )
    title: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    body: Mapped[str | None] = mapped_column(sa.Text)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    read_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (
        sa.Index(
            "ix_notifications_unread",
            "user_id",
            sa.desc("created_at"),
            postgresql_where=sa.text("read_at IS NULL"),
        ),
    )


class AuditLog(Base):
    """Кто из операторов панели что изменил (ТЗ §35)."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(sa.String(64))
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(sa.String(512))
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        sa.Index("ix_audit_logs_created_at", sa.desc("created_at")),
        sa.Index("ix_audit_logs_entity", "entity_type", "entity_id"),
        sa.Index("ix_audit_logs_actor_user_id", "actor_user_id"),
    )
