"""Сценарии — конфигурация поведения AI (ТЗ §13)."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Scenario(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "scenarios"

    name: Mapped[str] = mapped_column(sa.String(120), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(sa.Text)
    system_prompt: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # Критерий отбора лида для анализатора: что считать НАСТОЯЩИМ запросом по
    # этой услуге, а что нет. Отдельно от system_prompt (тот — персона для
    # генерации): по нему анализатор решает «отвечать или нет», без лишней
    # лояльности генеративного промпта. Пусто — анализатор берёт system_prompt.
    lead_criteria: Mapped[str | None] = mapped_column(sa.Text)

    model: Mapped[str | None] = mapped_column(sa.String(120))
    temperature: Mapped[float | None] = mapped_column(sa.Numeric(3, 2))
    max_tokens: Mapped[int | None] = mapped_column(sa.Integer)

    language: Mapped[str | None] = mapped_column(sa.String(8))
    tone: Mapped[str | None] = mapped_column(sa.String(64))
    max_reply_length: Mapped[int | None] = mapped_column(sa.Integer)
    context_messages: Mapped[int | None] = mapped_column(sa.Integer)

    knowledge_base_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("knowledge_bases.id", ondelete="SET NULL")
    )
    # Если ответ не подтверждён базой знаний, генерации не будет: выдаём
    # fallback_text или передаём человеку (ТЗ §17 — «AI не должен выдумывать»).
    require_knowledge_grounding: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False
    )
    fallback_text: Mapped[str | None] = mapped_column(sa.Text)

    human_handoff_enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)

    # Ответ в группе + личка: короткая фраза уходит в чат, полноценный
    # ИИ-ответ — в личку автору. Для лички правило работает как обычно.
    reply_in_dm: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    group_ack_text: Mapped[str | None] = mapped_column(sa.Text)
    # id топика в форум-группе бота-уведомлений: создаётся лениво, один раз.
    notify_topic_id: Mapped[int | None] = mapped_column(sa.BigInteger)
    min_confidence: Mapped[float | None] = mapped_column(sa.Numeric(3, 2))

    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)

    __table_args__ = (
        sa.CheckConstraint(
            "temperature IS NULL OR (temperature >= 0 AND temperature <= 2)",
            name="temperature_range",
        ),
        sa.CheckConstraint(
            "min_confidence IS NULL OR (min_confidence >= 0 AND min_confidence <= 1)",
            name="min_confidence_range",
        ),
        sa.CheckConstraint(
            "context_messages IS NULL OR context_messages > 0", name="context_messages_positive"
        ),
        sa.Index("ix_scenarios_enabled", "enabled"),
    )
