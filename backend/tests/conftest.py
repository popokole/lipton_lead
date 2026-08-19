"""Общие фикстуры тестов."""

from __future__ import annotations

import base64
import os
from typing import Any

import pytest

from app.core.config import Settings

# Ключ фиксированный и заведомо тестовый: настоящие ключи в тестах не нужны.
TEST_ENCRYPTION_KEY = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()

_REQUIRED: dict[str, Any] = {
    "database_url": "postgresql+asyncpg://tgai:pwd@localhost:5432/tgai",
    "redis_url": "redis://localhost:6379/0",
    "jwt_secret": "test-jwt-secret-that-is-long-enough-32ch",
    "session_encryption_key": TEST_ENCRYPTION_KEY,
    "admin_email": "admin@example.com",
    "admin_password": "test-admin-password",
}


def make_settings(**overrides: Any) -> Settings:
    """Собирает Settings без чтения окружения и .env."""
    return Settings(**{**_REQUIRED, **overrides})  # type: ignore[arg-type]


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def integration_settings() -> Settings:
    """Настройки под реально запущенные PostgreSQL и Redis."""
    return make_settings(
        database_url=os.environ.get("DATABASE_URL", _REQUIRED["database_url"]),
        redis_url=os.environ.get("REDIS_URL", _REQUIRED["redis_url"]),
        log_format="console",
        # Тесты логинятся десятки раз подряд с одного адреса. Ограничитель
        # частоты проверяется отдельным тестом с собственным низким лимитом.
        rate_limit_login_per_minute=10_000,
    )
