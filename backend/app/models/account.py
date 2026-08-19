"""Telegram-аккаунты, их сессии и прокси.

Сессия вынесена в отдельную таблицу намеренно: у неё другой режим доступа
(её читает только воркер), другой уровень чувствительности и другой срок
жизни, чем у карточки аккаунта. В ответах API она не участвует вообще.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models._types import Ciphertext, TelegramId, pg_enum
from app.models.enums import AccountStatus, SessionKind


class Proxy(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Сетевая настройка аккаунта.

    Прокси здесь — способ дотянуться до Telegram из сети, где он недоступен
    напрямую. Ротации прокси между аккаунтами нет и не будет: это средство
    обхода ограничений, а не сетевая настройка.
    """

    __tablename__ = "proxies"

    name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    scheme: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    host: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    port: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    username: Mapped[str | None] = mapped_column(sa.String(255))
    password_ct: Mapped[bytes | None] = mapped_column(Ciphertext)
    password_nonce: Mapped[bytes | None] = mapped_column(Ciphertext)
    password_key_id: Mapped[str | None] = mapped_column(sa.String(32))
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)

    __table_args__ = (
        sa.CheckConstraint("port > 0 AND port <= 65535", name="port_range"),
        sa.CheckConstraint(
            "scheme IN ('socks5', 'socks4', 'http', 'mtproxy')", name="scheme_supported"
        ),
    )


class TelegramSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Зашифрованная сессия Telethon.

    В ТЗ §31 модель названа Session — здесь TelegramSession, чтобы имя не
    сталкивалось с сессией SQLAlchemy в каждом импорте.

    Открытого текста в таблице нет: `ciphertext` расшифровывается ключом из
    окружения (SecretBox), а `key_id` позволяет ротировать ключ, не трогая
    старые записи. AAD привязывает шифротекст к `account_id`, поэтому строку
    нельзя переставить другому аккаунту.
    """

    __tablename__ = "telegram_sessions"

    account_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        sa.ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    kind: Mapped[SessionKind] = mapped_column(
        pg_enum(SessionKind, "session_kind"), nullable=False, default=SessionKind.STRING
    )
    ciphertext: Mapped[bytes] = mapped_column(Ciphertext, nullable=False)
    nonce: Mapped[bytes] = mapped_column(Ciphertext, nullable=False)
    key_id: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    alg: Mapped[str] = mapped_column(sa.String(32), nullable=False, default="AES-256-GCM")

    # Собственные api_id / api_hash аккаунта, тоже зашифрованные.
    api_id_ct: Mapped[bytes | None] = mapped_column(Ciphertext)
    api_id_nonce: Mapped[bytes | None] = mapped_column(Ciphertext)
    api_hash_ct: Mapped[bytes | None] = mapped_column(Ciphertext)
    api_hash_nonce: Mapped[bytes | None] = mapped_column(Ciphertext)

    rotated_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class Account(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Подключённый Telegram-аккаунт."""

    __tablename__ = "accounts"

    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    label: Mapped[str] = mapped_column(sa.String(120), nullable=False)

    tg_user_id: Mapped[int | None] = mapped_column(TelegramId)
    username: Mapped[str | None] = mapped_column(sa.String(64))
    display_name: Mapped[str | None] = mapped_column(sa.String(255))
    # Телефон нужен для повторной авторизации; в логи попадает только маской.
    phone_e164: Mapped[str | None] = mapped_column(sa.String(20))

    status: Mapped[AccountStatus] = mapped_column(
        pg_enum(AccountStatus, "account_status"), nullable=False, default=AccountStatus.CREATED
    )
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)

    proxy_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("proxies.id", ondelete="SET NULL")
    )

    # Аренда: аккаунт обслуживает ровно один воркер. Источник истины — Redis-лок,
    # эти поля нужны панели, чтобы показать, кто и до какого момента им владеет.
    worker_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("workers.id", ondelete="SET NULL")
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    last_seen_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    last_error_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (
        sa.Index(
            "uq_accounts_tg_user_id",
            "tg_user_id",
            unique=True,
            postgresql_where=sa.text("tg_user_id IS NOT NULL"),
        ),
        sa.Index("ix_accounts_status", "status"),
        sa.Index("ix_accounts_worker_id", "worker_id"),
        sa.Index("ix_accounts_owner_user_id", "owner_user_id"),
    )
