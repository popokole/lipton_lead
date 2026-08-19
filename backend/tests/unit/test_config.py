from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from app.core.config import decode_session_key
from tests.conftest import make_settings


class TestSessionKey:
    def test_accepts_base64(self) -> None:
        raw = base64.b64encode(b"a" * 32).decode()
        assert decode_session_key(raw) == b"a" * 32

    def test_accepts_urlsafe_base64(self) -> None:
        raw = base64.urlsafe_b64encode(bytes(range(32))).decode()
        assert decode_session_key(raw) == bytes(range(32))

    def test_accepts_hex(self) -> None:
        assert decode_session_key("ab" * 32) == bytes.fromhex("ab" * 32)

    @pytest.mark.parametrize("raw", ["", "short", base64.b64encode(b"a" * 16).decode()])
    def test_rejects_wrong_length(self, raw: str) -> None:
        with pytest.raises(ValueError, match="SESSION_ENCRYPTION_KEY"):
            decode_session_key(raw)

    def test_settings_rejects_bad_key(self) -> None:
        with pytest.raises(ValidationError):
            make_settings(session_encryption_key="not-a-key")


class TestDatabaseUrl:
    def test_requires_asyncpg_driver(self) -> None:
        with pytest.raises(ValidationError, match="asyncpg"):
            make_settings(database_url="postgresql://tgai:pwd@localhost/tgai")

    def test_accepts_asyncpg_driver(self) -> None:
        settings = make_settings()
        assert settings.database_url.startswith("postgresql+asyncpg://")


class TestInvariants:
    def test_lease_must_outlive_heartbeat(self) -> None:
        with pytest.raises(ValidationError, match="ACCOUNT_LEASE_TTL_SECONDS"):
            make_settings(account_lease_ttl_seconds=10, worker_heartbeat_seconds=10)

    def test_chunk_overlap_smaller_than_chunk(self) -> None:
        with pytest.raises(ValidationError, match="KB_CHUNK_OVERLAP"):
            make_settings(kb_chunk_size=200, kb_chunk_overlap=200)

    def test_reconnect_delays_ordered(self) -> None:
        with pytest.raises(ValidationError, match="TELEGRAM_RECONNECT_MAX_DELAY"):
            make_settings(telegram_reconnect_base_delay=10, telegram_reconnect_max_delay=5)

    def test_production_rejects_debug(self) -> None:
        with pytest.raises(ValidationError, match="DEBUG"):
            make_settings(env="prod", debug=True)

    def test_production_requires_long_jwt_secret(self) -> None:
        with pytest.raises(ValidationError, match="JWT_SECRET"):
            make_settings(env="prod", jwt_secret="short")

    def test_dev_allows_short_jwt_secret(self) -> None:
        assert make_settings(env="dev", jwt_secret="short").env == "dev"


class TestParsing:
    def test_cors_origins_split_from_string(self) -> None:
        settings = make_settings(cors_origins="http://a.local, http://b.local ,")
        assert settings.cors_origins == ["http://a.local", "http://b.local"]

    def test_log_level_normalised(self) -> None:
        assert make_settings(log_level="debug").log_level == "DEBUG"

    def test_unknown_log_level_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_settings(log_level="verbose")

    def test_secrets_are_not_in_repr(self) -> None:
        text = repr(make_settings())
        assert "test-admin-password" not in text
        assert "test-jwt-secret" not in text

    def test_kb_size_helper(self) -> None:
        assert make_settings(kb_max_file_size_mb=3).kb_max_file_size_bytes == 3 * 1024 * 1024
