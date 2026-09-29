"""Логи не должны содержать секретов (ТЗ §30)."""

from __future__ import annotations

import logging

import pytest
import structlog

from app.core.logging import (
    REDACTED,
    SecretScrubFilter,
    configure_logging,
    mask_phone,
    redact_processor,
)
from tests.conftest import make_settings

BOT_URL = "https://api.telegram.org/bot123456789:AAH-secret_Token/sendMessage"


def process(**fields: object) -> dict[str, object]:
    return dict(redact_processor(None, "info", dict(fields)))


class TestSensitiveKeys:
    @pytest.mark.parametrize(
        "key",
        [
            "session",
            "session_string",
            "auth_key",
            "api_hash",
            "password",
            "phone_code",
            "phone_code_hash",
            "access_token",
            "openai_api_key",
            "authorization",
            "proxy_password",
        ],
    )
    def test_known_key_is_redacted(self, key: str) -> None:
        assert process(**{key: "secret-value"})[key] == REDACTED

    @pytest.mark.parametrize(
        "key", ["user_password", "TELEGRAM_API_HASH", "refresh_token_value", "my_secret_thing"]
    )
    def test_substring_match_is_redacted(self, key: str) -> None:
        assert process(**{key: "secret-value"})[key] == REDACTED

    def test_harmless_keys_pass_through(self) -> None:
        result = process(account_id="acc-1", chat_id=42, event_type="MESSAGE_RECEIVED")
        assert result == {"account_id": "acc-1", "chat_id": 42, "event_type": "MESSAGE_RECEIVED"}


class TestNestedStructures:
    def test_nested_dict_is_redacted(self) -> None:
        result = process(payload={"api_hash": "h", "chat_id": 5})
        assert result["payload"] == {"api_hash": REDACTED, "chat_id": 5}

    def test_list_of_dicts_is_redacted(self) -> None:
        result = process(items=[{"password": "p"}, {"name": "ok"}])
        assert result["items"] == [{"password": REDACTED}, {"name": "ok"}]

    def test_deeply_nested_is_redacted(self) -> None:
        result = process(a={"b": {"c": {"session": "s", "keep": 1}}})
        assert result["a"]["b"]["c"] == {"session": REDACTED, "keep": 1}  # type: ignore[index]


class TestFreeText:
    def test_session_string_in_message_is_scrubbed(self) -> None:
        session = "1" + "A" * 60
        result = process(event=f"failed to load {session} for account")
        assert session not in str(result["event"])
        assert REDACTED in str(result["event"])

    def test_bytes_are_summarised_not_dumped(self) -> None:
        result = process(blob=b"\x00\x01\x02\x03")
        assert result["blob"] == "<4 bytes>"

    def test_long_values_are_truncated(self) -> None:
        result = process(text="x" * 10_000)
        assert len(str(result["text"])) < 5_000


class TestBotToken:
    def test_bot_token_in_free_text_is_scrubbed(self) -> None:
        result = process(event=f"POST {BOT_URL} failed")
        assert "AAH-secret_Token" not in str(result["event"])
        assert f"/bot{REDACTED}/sendMessage" in str(result["event"])

    def test_stdlib_records_are_scrubbed(self) -> None:
        """httpx пишет URL Bot API (с токеном) через stdlib logging, мимо structlog."""
        record = logging.LogRecord(
            "httpx",
            logging.INFO,
            __file__,
            1,
            'HTTP Request: %s %s "%s"',
            ("POST", BOT_URL, "200 OK"),
            None,
        )
        assert SecretScrubFilter().filter(record)
        assert "AAH-secret_Token" not in record.getMessage()
        assert f"/bot{REDACTED}/sendMessage" in record.getMessage()

    def test_http_client_request_lines_are_quiet_at_info(self) -> None:
        root = logging.getLogger()
        handlers, level = list(root.handlers), root.level
        try:
            configure_logging(make_settings(log_level="INFO"))
            assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
            assert logging.getLogger("httpcore").getEffectiveLevel() == logging.WARNING
            assert any(
                isinstance(item, SecretScrubFilter)
                for handler in root.handlers
                for item in handler.filters
            )
        finally:
            root.handlers[:] = handlers
            root.setLevel(level)
            structlog.reset_defaults()


class TestMaskPhone:
    def test_keeps_country_code_and_tail(self) -> None:
        assert mask_phone("+79991234567") == "+79*******67"

    def test_short_input_fully_masked(self) -> None:
        assert mask_phone("123") == REDACTED

    def test_none_passes_through(self) -> None:
        assert mask_phone(None) is None
