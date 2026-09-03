"""Личность ИИ-собеседника (глобальная).

Одна «личность» на всю систему: описание характера и примеры того, как этот
человек пишет. Подмешивается в КАЖДУЮ генерацию поверх промпта сценария —
сценарий отвечает за то, ЧТО и кому отвечать, личность за то, КАК это звучит.

Одна строка на всю систему, как и настройки бота-уведомлений: фиксированный id
вместо автоинкремента избавляет от гонки «создать/обновить».
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin

# Единственная строка: детерминированный ключ.
SINGLETON_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


class Persona(TimestampMixin, Base):
    __tablename__ = "persona"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=SINGLETON_ID
    )
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)

    # Кто этот человек: характер, тон, привычки речи, что можно/нельзя.
    character: Mapped[str | None] = mapped_column(sa.Text)
    # Примеры переписки — «история» того, как он пишет. Few-shot, задаёт стиль.
    examples: Mapped[str | None] = mapped_column(sa.Text)

    # Глобальный базовый промпт («в целом»): общие правила ответа для ВСЕХ
    # сценариев. Если задан — заменяет зашитые GENERATOR_BASE_RULES. Применяется
    # всегда, независимо от переключателя личности.
    base_rules: Mapped[str | None] = mapped_column(sa.Text)
    # Глобальная максимальная длина ответа (символов). Используется, когда у
    # сценария своя длина не задана. Пусто — без общего ограничения.
    max_reply_length: Mapped[int | None] = mapped_column(sa.Integer)
