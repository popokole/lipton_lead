"""Пользователи панели (не Telegram-аккаунты)."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models._types import pg_enum
from app.models.enums import UserRole


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(sa.String(320), unique=True, nullable=False)
    # Argon2id; сам хеш наружу не отдаётся ни в одной схеме ответа.
    password_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    full_name: Mapped[str | None] = mapped_column(sa.String(200))
    role: Mapped[UserRole] = mapped_column(
        pg_enum(UserRole, "user_role"), nullable=False, default=UserRole.VIEWER
    )
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (sa.Index("ix_users_role", "role"),)
