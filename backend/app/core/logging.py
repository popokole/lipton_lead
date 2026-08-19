"""Структурированное логирование.

Единственный вход в логи всего приложения. Ключевое требование ТЗ §30:
в логи никогда не должны попадать сессии, auth key, api_hash, пароли и коды
входа Telegram. Редакция выполняется процессором, а не силой воли вызывающего
кода: любое поле с «опасным» именем вырезается автоматически.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

from app.core.config import Settings

REDACTED = "[redacted]"

# Имена полей, значение которых нельзя писать в лог ни при каких условиях.
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "session",
        "session_string",
        "string_session",
        "session_blob",
        "ciphertext",
        "auth_key",
        "authkey",
        "api_hash",
        "api_id_ct",
        "api_hash_ct",
        "password",
        "password_hash",
        "passwd",
        "pwd",
        "twofa",
        "two_fa",
        "2fa",
        "code",
        "login_code",
        "phone_code",
        "phone_code_hash",
        "token",
        "access_token",
        "refresh_token",
        "jwt",
        "secret",
        "secret_key",
        "encryption_key",
        "session_encryption_key",
        "openai_api_key",
        "api_key",
        "authorization",
        "cookie",
        "set-cookie",
        "proxy_password",
    }
)

# Подстроки в имени поля, которых достаточно для редакции.
SENSITIVE_SUBSTRINGS: tuple[str, ...] = ("password", "secret", "token", "api_hash", "auth_key")

# Строковая сессия Telethon, попавшая в свободный текст сообщения.
_SESSION_STRING_RE = re.compile(r"\b1[A-Za-z0-9+/=_-]{40,}\b")

_MAX_VALUE_LENGTH = 4096


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    if lowered in SENSITIVE_KEYS:
        return True
    return any(marker in lowered for marker in SENSITIVE_SUBSTRINGS)


def _scrub_text(value: str) -> str:
    scrubbed = _SESSION_STRING_RE.sub(REDACTED, value)
    return scrubbed


def mask_phone(phone: str | None) -> str | None:
    """Оставляет код страны и две последние цифры: +7*******89."""
    if not phone:
        return phone
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 5:
        return REDACTED
    return f"+{digits[:2]}{'*' * (len(digits) - 4)}{digits[-2:]}"


def _redact_value(key: str, value: Any, depth: int = 0) -> Any:
    if _is_sensitive(key):
        return REDACTED
    if depth > 6:
        return value
    if isinstance(value, MutableMapping):
        return {k: _redact_value(str(k), v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return type(value)(_redact_value(key, item, depth + 1) for item in value)
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, str):
        text = _scrub_text(value)
        if len(text) > _MAX_VALUE_LENGTH:
            return text[:_MAX_VALUE_LENGTH] + "…"
        return text
    return value


def redact_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    return {key: _redact_value(str(key), value) for key, value in event_dict.items()}


def configure_logging(settings: Settings) -> None:
    """Настраивает structlog и stdlib logging в один поток вывода."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, settings.log_level),
        force=True,
    )
    for noisy in ("uvicorn.access", "sqlalchemy.engine.Engine", "telethon"):
        logging.getLogger(noisy).setLevel(
            logging.WARNING if settings.log_level == "INFO" else logging.INFO
        )

    renderer: Any
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]
    if settings.log_format == "json":
        # В JSON трейсбек нужно превратить в строку самим.
        processors.append(structlog.processors.format_exc_info)
        renderer = structlog.processors.JSONRenderer(ensure_ascii=False)
    else:
        # ConsoleRenderer печатает исключения сам и лучше: format_exc_info
        # здесь только мешает.
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    processors.extend([redact_processor, renderer])

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, settings.log_level)),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


def bind_request_context(**fields: Any) -> None:
    """Привязывает поля ко всем логам текущей задачи (request_id, account_id...)."""
    structlog.contextvars.bind_contextvars(**fields)


def clear_request_context() -> None:
    structlog.contextvars.clear_contextvars()
