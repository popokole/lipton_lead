"""Нормализация событий Telegram (ТЗ §6, §7)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.models import ChatType, MediaType
from app.telegram.messages import MessageNormalizer
from tests.builders import FakeEvent

ACCOUNT = uuid.uuid4()


def normalize(event: FakeEvent, max_length: int = 4000):
    return MessageNormalizer(max_length).normalize(ACCOUNT, event)


class TestBasicFields:
    def test_extracts_identifiers_and_text(self) -> None:
        result = normalize(FakeEvent(message_id=42, chat_id=-100500, text="  привет  "))

        assert result.account_id == ACCOUNT
        assert result.tg_message_id == 42
        assert result.tg_chat_id == -100500
        assert result.text == "привет"
        assert result.dedup_key == (ACCOUNT, -100500, 42)

    def test_incoming_and_outgoing_are_exclusive(self) -> None:
        incoming = normalize(FakeEvent(out=False))
        outgoing = normalize(FakeEvent(out=True))

        assert (incoming.is_incoming, incoming.is_outgoing) == (True, False)
        assert (outgoing.is_incoming, outgoing.is_outgoing) == (False, True)

    def test_sender_details(self) -> None:
        result = normalize(FakeEvent(sender_id=777, first_name="Иван", last_name="Петров"))

        assert result.sender_tg_id == 777
        assert result.sender_username == "client"
        assert result.sender_display_name == "Иван Петров"

    def test_reply_and_forward_flags(self) -> None:
        result = normalize(FakeEvent(reply_to_msg_id=11, fwd_from=object()))

        assert result.reply_to_tg_message_id == 11
        assert result.is_forwarded is True

    def test_naive_date_is_treated_as_utc(self) -> None:
        result = normalize(FakeEvent(date=datetime(2026, 8, 18, 12, 0)))
        assert result.date == datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


class TestChatType:
    def test_private(self) -> None:
        result = normalize(FakeEvent(is_private=True, is_group=False))
        assert result.chat_type is ChatType.PRIVATE
        assert result.is_private is True

    def test_basic_group(self) -> None:
        assert normalize(FakeEvent(is_group=True)).chat_type is ChatType.GROUP

    def test_supergroup_is_channel_with_megagroup_flag(self) -> None:
        event = FakeEvent(is_group=False, is_channel=True, megagroup=True)
        assert normalize(event).chat_type is ChatType.SUPERGROUP

    def test_broadcast_channel(self) -> None:
        event = FakeEvent(is_group=False, is_channel=True, megagroup=False)
        assert normalize(event).chat_type is ChatType.CHANNEL


class TestMedia:
    def test_no_media(self) -> None:
        assert normalize(FakeEvent()).media_type is None

    @pytest.mark.parametrize(
        ("kind", "expected"),
        [
            ("photo", MediaType.PHOTO),
            ("voice", MediaType.VOICE),
            ("video", MediaType.VIDEO),
            ("audio", MediaType.AUDIO),
            ("sticker", MediaType.STICKER),
            ("poll", MediaType.POLL),
            ("document", MediaType.DOCUMENT),
        ],
    )
    def test_recognised_kinds(self, kind: str, expected: MediaType) -> None:
        assert normalize(FakeEvent(media_kind=kind)).media_type is expected

    def test_unknown_media_falls_back_to_other(self) -> None:
        assert normalize(FakeEvent(media=object())).media_type is MediaType.OTHER


class TestTextHandling:
    def test_long_text_is_truncated_to_the_configured_limit(self) -> None:
        result = normalize(FakeEvent(text="я" * 500), max_length=100)
        assert len(result.text) == 100

    def test_empty_text_is_reported_by_has_text(self) -> None:
        assert normalize(FakeEvent(text="   ")).has_text is False
        assert normalize(FakeEvent(text="есть текст")).has_text is True

    def test_log_view_omits_message_text(self) -> None:
        result = normalize(FakeEvent(text="секретный текст клиента"))
        view = result.for_log()

        assert "секретный текст клиента" not in str(view)
        assert view["text_length"] == len("секретный текст клиента")
        assert view["message_id"] == 555


class TestBrokenEvents:
    def test_missing_message_id_is_rejected(self) -> None:
        event = FakeEvent()
        event.message.id = None  # type: ignore[assignment]

        with pytest.raises(ValueError, match="message id"):
            normalize(event)

    def test_missing_date_is_rejected(self) -> None:
        event = FakeEvent()
        event.message.date = None  # type: ignore[assignment]

        with pytest.raises(ValueError, match="date"):
            normalize(event)

    def test_missing_chat_id_is_rejected(self) -> None:
        event = FakeEvent()
        event.chat_id = None  # type: ignore[assignment]
        event.message.chat_id = None  # type: ignore[assignment]

        with pytest.raises(ValueError, match="chat id"):
            normalize(event)
