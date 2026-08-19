"""Хранение Telegram-сессий (ТЗ §4).

Внутренний формат один — StringSession Telethon. Всё остальное (файловая
сессия, tdata) конвертируется в него отдельным адаптером на входе и внутрь
системы уже не проникает.

В базе лежит только шифротекст. Ключ живёт в окружении процесса, AAD
привязывает запись к конкретному аккаунту: переставить строку другому
аккаунту, даже имея доступ к базе, не получится — расшифровка не пройдёт
проверку подлинности.

Ни одно значение отсюда не должно попасть в лог или в ответ API. Поэтому
`SessionCredentials` не имеет читаемого repr, а API-схемы аккаунта не содержат
полей сессии в принципе.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon.sessions import StringSession

from app.core.clock import utcnow
from app.core.crypto import EncryptedBlob, SecretBox
from app.core.logging import get_logger
from app.models import SessionKind, TelegramSession

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SessionCredentials:
    """Всё, что нужно, чтобы поднять клиента. Секретно целиком."""

    session_string: str
    api_id: int
    api_hash: str

    def __repr__(self) -> str:
        return (
            f"SessionCredentials(api_id={self.api_id}, session=<{len(self.session_string)} chars>)"
        )


class SessionAdapter(Protocol):
    """Преобразование внешнего формата сессии во внутренний."""

    kind: SessionKind

    def to_string(self, raw: bytes | str) -> str:
        """Переводит внешнее представление в StringSession."""

    def validate(self, session_string: str) -> bool:
        """Проверяет, что строка вообще разбирается как сессия."""


class StringSessionAdapter:
    """Собственный формат: строка Telethon как есть."""

    kind = SessionKind.STRING

    def to_string(self, raw: bytes | str) -> str:
        value = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        value = value.strip()
        if not self.validate(value):
            raise ValueError("provided value is not a valid Telethon StringSession")
        return value

    def validate(self, session_string: str) -> bool:
        try:
            StringSession(session_string)
        except (ValueError, TypeError, UnicodeDecodeError):
            return False
        return True


class FileSessionAdapter:
    """Файл `.session` Telethon (SQLite) → StringSession.

    Файл читается один раз при импорте: дальше система работает только со
    строкой, а файл нигде не хранится.
    """

    kind = SessionKind.FILE

    def to_string(self, raw: bytes | str) -> str:
        if isinstance(raw, str):
            raise TypeError("file session must be provided as bytes")
        return _sqlite_session_to_string(raw)

    def validate(self, session_string: str) -> bool:
        return StringSessionAdapter().validate(session_string)


def _sqlite_session_to_string(payload: bytes) -> str:
    """Достаёт auth_key из SQLite-файла Telethon и собирает StringSession."""
    import sqlite3
    import tempfile
    from pathlib import Path

    from telethon.crypto import AuthKey

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "imported.session"
        path.write_bytes(payload)
        connection = sqlite3.connect(str(path))
        try:
            row = connection.execute(
                "SELECT dc_id, server_address, port, auth_key FROM sessions LIMIT 1"
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise ValueError("file is not a Telethon session database") from exc
        finally:
            connection.close()

    if row is None:
        raise ValueError("session file contains no authorised session")

    dc_id, server_address, port, auth_key = row
    session = StringSession()
    session.set_dc(dc_id, server_address, port)
    session.auth_key = AuthKey(data=auth_key)
    return session.save()


class SessionManager:
    """Чтение и запись зашифрованных сессий.

    Транзакцией управляет вызывающий код: менеджер получает готовую сессию
    SQLAlchemy и ничего не коммитит сам.
    """

    def __init__(self, box: SecretBox) -> None:
        self._box = box

    @staticmethod
    def _aad(account_id: uuid.UUID) -> str:
        # Привязка шифротекста к владельцу: подмена строки в БД не сработает.
        return f"account:{account_id}"

    async def load(self, db: AsyncSession, account_id: uuid.UUID) -> SessionCredentials | None:
        row = await db.scalar(
            select(TelegramSession).where(
                TelegramSession.account_id == account_id,
                TelegramSession.revoked_at.is_(None),
            )
        )
        if row is None:
            return None

        aad = self._aad(account_id)
        session_string = self._box.decrypt_str(
            EncryptedBlob(row.ciphertext, row.nonce, row.key_id, row.alg), aad=aad
        )
        if row.api_id_ct is None or row.api_hash_ct is None:
            raise ValueError(f"session {row.id} has no stored api credentials")

        api_id = int(
            self._box.decrypt_str(
                EncryptedBlob(row.api_id_ct, row.api_id_nonce or b"", row.key_id, row.alg), aad=aad
            )
        )
        api_hash = self._box.decrypt_str(
            EncryptedBlob(row.api_hash_ct, row.api_hash_nonce or b"", row.key_id, row.alg), aad=aad
        )
        return SessionCredentials(session_string=session_string, api_id=api_id, api_hash=api_hash)

    async def store(
        self,
        db: AsyncSession,
        account_id: uuid.UUID,
        *,
        session_string: str,
        api_id: int,
        api_hash: str,
        kind: SessionKind = SessionKind.STRING,
    ) -> None:
        aad = self._aad(account_id)
        session_blob = self._box.encrypt(session_string, aad=aad)
        api_id_blob = self._box.encrypt(str(api_id), aad=aad)
        api_hash_blob = self._box.encrypt(api_hash, aad=aad)

        row = await db.scalar(
            select(TelegramSession).where(TelegramSession.account_id == account_id)
        )
        if row is None:
            row = TelegramSession(account_id=account_id, kind=kind)
            db.add(row)

        row.kind = kind
        row.ciphertext = session_blob.ciphertext
        row.nonce = session_blob.nonce
        row.key_id = session_blob.key_id
        row.alg = session_blob.alg
        row.api_id_ct = api_id_blob.ciphertext
        row.api_id_nonce = api_id_blob.nonce
        row.api_hash_ct = api_hash_blob.ciphertext
        row.api_hash_nonce = api_hash_blob.nonce
        row.rotated_at = utcnow()
        row.revoked_at = None

        await db.flush()
        logger.info("session_stored", account_id=str(account_id), key_id=session_blob.key_id)

    def decrypt_secret(self, ciphertext: bytes, nonce: bytes, key_id: str) -> str:
        """Расшифровывает мелкие секреты рядом с сессиями — например пароль прокси."""
        return self._box.decrypt_str(
            EncryptedBlob(ciphertext, nonce, key_id, "AES-256-GCM"), aad="proxy"
        )

    async def revoke(self, db: AsyncSession, account_id: uuid.UUID) -> None:
        """Помечает сессию недействительной, не удаляя запись.

        Разлогин и удаление аккаунта — разные события: после разлогина у нас
        остаётся история и настройки, но подключиться уже нельзя.
        """
        row = await db.scalar(
            select(TelegramSession).where(TelegramSession.account_id == account_id)
        )
        if row is None:
            return
        row.revoked_at = utcnow()
        await db.flush()
        logger.info("session_revoked", account_id=str(account_id))

    async def delete(self, db: AsyncSession, account_id: uuid.UUID) -> None:
        row = await db.scalar(
            select(TelegramSession).where(TelegramSession.account_id == account_id)
        )
        if row is None:
            return
        await db.delete(row)
        await db.flush()
        logger.info("session_deleted", account_id=str(account_id))
