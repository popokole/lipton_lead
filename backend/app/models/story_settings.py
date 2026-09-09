"""Настройки авто-просмотра историй (Stories) — прогрев аудитории.

Одна строка на всю систему (как notify/persona). Аккаунт периодически
«просматривает» истории лидов и недавно активных в отслеживаемых чатах, чтобы
появляться у них в списке зрителей. Темп регулируется из панели: массовый
просмотр — заметный ботский сигнал, поэтому по умолчанию выключено и медленно.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin

SINGLETON_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")


class StorySettings(TimestampMixin, Base):
    __tablename__ = "story_settings"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=SINGLETON_ID
    )
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)

    # Темп: сколько людей в час максимум просматривать. Анти-бан — держать низким.
    per_hour: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=20)
    # «Активный» = писал в отслеживаемых чатах за столько последних часов.
    active_window_hours: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=48)
    # Не смотреть одного и того же человека чаще, чем раз в столько часов.
    per_user_cooldown_hours: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=20)
