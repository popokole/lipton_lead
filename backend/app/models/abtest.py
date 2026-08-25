"""A/B заходов: несколько вариантов ПЕРВОГО сообщения лиду.

Когда у сценария включён A/B, первый ответ новому собеседнику берётся не из ИИ,
а из одного варианта-захода (по очереди — реже всего отправленный). Платформа
считает отправки и ответы на каждый вариант — видно, какой заход конвертит.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AbVariant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "ab_variants"

    scenario_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("scenarios.id", ondelete="CASCADE"), nullable=False
    )
    text: Mapped[str] = mapped_column(sa.Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)

    # Сколько раз этот заход отправлен и на сколько из них собеседник ответил.
    sent_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    reply_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (sa.Index("ix_ab_variants_scenario_id", "scenario_id"),)
