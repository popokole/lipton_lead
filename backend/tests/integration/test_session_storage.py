"""Хранение сессий: шифрование, привязка к владельцу, отзыв (ТЗ §4)."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import DecryptionError, SecretBox
from app.models import Account, AccountStatus, TelegramSession
from app.telegram.session_manager import (
    FileSessionAdapter,
    SessionManager,
    StringSessionAdapter,
)
from tests.fakes import make_session_string

pytestmark = pytest.mark.integration

KEY_A = b"A" * 32
KEY_B = b"B" * 32
SESSION = make_session_string()
API_ID = 12345
API_HASH = "api-hash-value"


def manager(key: bytes = KEY_A, key_id: str = "k1") -> SessionManager:
    return SessionManager(SecretBox(key_id, {key_id: key}))


async def make_account(db: AsyncSession, label: str, tg_user_id: int) -> Account:
    account = Account(label=label, tg_user_id=tg_user_id, status=AccountStatus.CREATED)
    db.add(account)
    await db.flush()
    return account


class TestRoundTrip:
    async def test_store_and_load(self, db: AsyncSession, account: Account) -> None:
        await manager().store(
            db, account.id, session_string=SESSION, api_id=API_ID, api_hash=API_HASH
        )

        loaded = await manager().load(db, account.id)

        assert loaded is not None
        assert loaded.session_string == SESSION
        assert loaded.api_id == API_ID
        assert loaded.api_hash == API_HASH

    async def test_missing_session_returns_none(self, db: AsyncSession, account: Account) -> None:
        assert await manager().load(db, account.id) is None

    async def test_store_twice_updates_the_same_row(
        self, db: AsyncSession, account: Account
    ) -> None:
        mgr = manager()
        await mgr.store(db, account.id, session_string=SESSION, api_id=API_ID, api_hash=API_HASH)
        second = make_session_string(dc_id=4)
        await mgr.store(db, account.id, session_string=second, api_id=999, api_hash="other")

        rows = list(
            (
                await db.scalars(
                    select(TelegramSession).where(TelegramSession.account_id == account.id)
                )
            ).all()
        )
        assert len(rows) == 1
        loaded = await mgr.load(db, account.id)
        assert loaded is not None
        assert loaded.session_string == second
        assert loaded.api_id == 999


class TestEncryption:
    async def test_plaintext_is_absent_from_the_database(
        self, db: AsyncSession, account: Account
    ) -> None:
        await manager().store(
            db, account.id, session_string=SESSION, api_id=API_ID, api_hash=API_HASH
        )

        row = await db.scalar(
            select(TelegramSession).where(TelegramSession.account_id == account.id)
        )
        assert row is not None
        assert SESSION.encode() not in row.ciphertext
        assert API_HASH.encode() not in (row.api_hash_ct or b"")
        assert row.alg == "AES-256-GCM"
        assert row.key_id == "k1"

    async def test_wrong_key_cannot_read_the_session(
        self, db: AsyncSession, account: Account
    ) -> None:
        await manager(KEY_A).store(
            db, account.id, session_string=SESSION, api_id=API_ID, api_hash=API_HASH
        )

        with pytest.raises(DecryptionError):
            await manager(KEY_B).load(db, account.id)

    async def test_session_cannot_be_moved_to_another_account(
        self, db: AsyncSession, account: Account
    ) -> None:
        """AAD привязывает шифротекст к аккаунту: перестановка строки не сработает."""
        mgr = manager()
        await mgr.store(db, account.id, session_string=SESSION, api_id=API_ID, api_hash=API_HASH)
        thief = await make_account(db, "thief", account.tg_user_id + 1)  # type: ignore[operator]

        row = await db.scalar(
            select(TelegramSession).where(TelegramSession.account_id == account.id)
        )
        assert row is not None
        row.account_id = thief.id
        await db.flush()

        with pytest.raises(DecryptionError):
            await mgr.load(db, thief.id)

    async def test_rotated_key_still_reads_old_records(
        self, db: AsyncSession, account: Account
    ) -> None:
        await manager(KEY_A, "k1").store(
            db, account.id, session_string=SESSION, api_id=API_ID, api_hash=API_HASH
        )

        rotated = SessionManager(SecretBox("k2", {"k2": KEY_B, "k1": KEY_A}))
        loaded = await rotated.load(db, account.id)

        assert loaded is not None
        assert loaded.session_string == SESSION


class TestRevocation:
    async def test_revoked_session_is_not_loaded(self, db: AsyncSession, account: Account) -> None:
        mgr = manager()
        await mgr.store(db, account.id, session_string=SESSION, api_id=API_ID, api_hash=API_HASH)
        await mgr.revoke(db, account.id)

        assert await mgr.load(db, account.id) is None

    async def test_revoked_row_survives_for_history(
        self, db: AsyncSession, account: Account
    ) -> None:
        mgr = manager()
        await mgr.store(db, account.id, session_string=SESSION, api_id=API_ID, api_hash=API_HASH)
        await mgr.revoke(db, account.id)

        row = await db.scalar(
            select(TelegramSession).where(TelegramSession.account_id == account.id)
        )
        assert row is not None
        assert row.revoked_at is not None

    async def test_new_login_after_revocation_reactivates_the_row(
        self, db: AsyncSession, account: Account
    ) -> None:
        mgr = manager()
        await mgr.store(db, account.id, session_string=SESSION, api_id=API_ID, api_hash=API_HASH)
        await mgr.revoke(db, account.id)
        await mgr.store(db, account.id, session_string=SESSION, api_id=API_ID, api_hash=API_HASH)

        assert await mgr.load(db, account.id) is not None

    async def test_delete_removes_the_row(self, db: AsyncSession, account: Account) -> None:
        mgr = manager()
        await mgr.store(db, account.id, session_string=SESSION, api_id=API_ID, api_hash=API_HASH)
        await mgr.delete(db, account.id)

        row = await db.scalar(
            select(TelegramSession).where(TelegramSession.account_id == account.id)
        )
        assert row is None


class TestAdapters:
    def test_string_adapter_accepts_valid_session(self) -> None:
        adapter = StringSessionAdapter()
        assert adapter.to_string(SESSION) == SESSION
        assert adapter.validate(SESSION) is True

    def test_string_adapter_rejects_garbage(self) -> None:
        adapter = StringSessionAdapter()
        assert adapter.validate("not-a-session") is False
        with pytest.raises(ValueError, match="StringSession"):
            adapter.to_string("not-a-session")

    def test_file_adapter_requires_bytes(self) -> None:
        with pytest.raises(TypeError, match="bytes"):
            FileSessionAdapter().to_string("path/to/file")

    def test_file_adapter_rejects_non_session_file(self) -> None:
        with pytest.raises(ValueError, match="Telethon session"):
            FileSessionAdapter().to_string(b"this is not a sqlite database")

    async def test_credentials_repr_hides_the_session(
        self, db: AsyncSession, account: Account
    ) -> None:
        mgr = manager()
        await mgr.store(db, account.id, session_string=SESSION, api_id=API_ID, api_hash=API_HASH)
        loaded = await mgr.load(db, account.id)

        assert loaded is not None
        assert SESSION not in repr(loaded)
        assert API_HASH not in repr(loaded)
