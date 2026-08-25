"""Стоп-лист: кому НИКОГДА не отвечаем (админы, конкуренты, боты, спамеры).

Сообщения таких отправителей сохраняются (для статистики и дерева), но правила
и ответы для них не запускаются. Проверка на горячем пути, поэтому в воркере
держится в памяти и обновляется пачкой, а не запросом на каждое сообщение.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, UUIDPrimaryKeyMixin
from app.models._types import TelegramId


class StopEntry(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "stoplist_entries"

    # Блокировка по tg-id и/или по @username (что известно оператору).
    tg_user_id: Mapped[int | None] = mapped_column(TelegramId)
    username: Mapped[str | None] = mapped_column(sa.String(64))
    note: Mapped[str | None] = mapped_column(sa.String(255))

    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (
        sa.Index("ix_stoplist_tg_user_id", "tg_user_id"),
        sa.Index("ix_stoplist_username", "username"),
    )
