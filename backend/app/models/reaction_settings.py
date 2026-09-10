"""Настройки авто-реакций на сообщения в чатах — мягкий прогрев/охват.

Одна строка на систему. Аккаунт периодически ставит эмодзи-реакцию на свежие
сообщения в отслеживаемых чатах. По умолчанию выключено и медленно: реакции
заметнее просмотра историй, легко перегнуть.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin

SINGLETON_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")


class ReactionSettings(TimestampMixin, Base):
    __tablename__ = "reaction_settings"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=SINGLETON_ID
    )
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    # Реакций в час максимум (потолок; реальный темп задают рандом-интервалы).
    per_hour: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=15)
    # Реагируем только на сообщения не старше стольких часов.
    fresh_window_hours: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=6)
    # Не чаще одной реакции в один чат за столько минут (не палимся в одном чате).
    per_chat_cooldown_minutes: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=30)
