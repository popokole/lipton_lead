"""Чаты, за которыми следит аккаунт."""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models._types import TelegramId, pg_enum
from app.models.enums import ChatType


class Chat(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Чат в разрезе аккаунта.

    Один и тот же Telegram-чат для двух аккаунтов — две разные строки: у них
    разные права доступа, разный статус мониторинга и разная история.
    """

    __tablename__ = "chats"

    account_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    tg_chat_id: Mapped[int] = mapped_column(TelegramId, nullable=False)
    type: Mapped[ChatType] = mapped_column(pg_enum(ChatType, "chat_type"), nullable=False)
    title: Mapped[str | None] = mapped_column(sa.String(255))
    username: Mapped[str | None] = mapped_column(sa.String(64))

    monitored: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    # Чат без анти-бан лимита: сюда можно отвечать без задержки «раз в N минут».
    cooldown_exempt: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    # Тест-чат: снимает ограничения (one_shot, анти-бан лимит, рабочие часы),
    # чтобы можно было проверять сценарии сколько угодно раз.
    test_mode: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    members_count: Mapped[int | None] = mapped_column(sa.Integer)
    last_message_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    # Аватар чата: кешируем байты, чтобы не дёргать Telegram на каждый показ
    # ленты. avatar_fetched_at отмечает попытку — в том числе неудачную,
    # чтобы не долбить сеть за фото, которого нет.
    avatar: Mapped[bytes | None] = mapped_column(sa.LargeBinary)
    avatar_mime: Mapped[str | None] = mapped_column(sa.String(32))
    avatar_fetched_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (
        sa.UniqueConstraint("account_id", "tg_chat_id", name="uq_chats_account_id_tg_chat_id"),
        sa.Index("ix_chats_monitored", "account_id", postgresql_where=sa.text("monitored IS TRUE")),
        sa.Index("ix_chats_tg_chat_id", "tg_chat_id"),
    )
