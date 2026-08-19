"""Нормализация сообщений Telegram (ТЗ §6).

Всё, что идёт дальше по конвейеру, работает с одним неизменяемым DTO, а не с
объектом Telethon. Причина простая: правила, AI и валидатор не должны знать,
как устроен Telethon, иначе обновление библиотеки ломает бизнес-логику, а
тесты требуют настоящего клиента.

Нормализатор работает с любым объектом, у которого есть нужные атрибуты, —
это позволяет проверять конвейер без подключения к Telegram.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.models import ChatType, MediaType

# Порядок важен: у видео-сообщения есть и document, и video, а осмысленный тип
# для оператора — более конкретный.
_MEDIA_ATTRIBUTES: tuple[tuple[str, MediaType], ...] = (
    ("photo", MediaType.PHOTO),
    ("voice", MediaType.VOICE),
    ("video_note", MediaType.VIDEO),
    ("video", MediaType.VIDEO),
    ("audio", MediaType.AUDIO),
    ("sticker", MediaType.STICKER),
    ("gif", MediaType.ANIMATION),
    ("poll", MediaType.POLL),
    ("geo", MediaType.LOCATION),
    ("contact", MediaType.CONTACT),
    ("web_preview", MediaType.WEBPAGE),
    ("document", MediaType.DOCUMENT),
)


@dataclass(frozen=True, slots=True)
class NormalizedMessage:
    """Сообщение в виде, пригодном для обработки."""

    account_id: uuid.UUID
    tg_chat_id: int
    tg_message_id: int
    chat_type: ChatType
    text: str
    date: datetime
    is_incoming: bool
    is_outgoing: bool
    sender_tg_id: int | None = None
    sender_username: str | None = None
    sender_display_name: str | None = None
    chat_title: str | None = None
    chat_username: str | None = None
    reply_to_tg_message_id: int | None = None
    is_forwarded: bool = False
    media_type: MediaType | None = None

    @property
    def has_text(self) -> bool:
        return bool(self.text.strip())

    @property
    def is_private(self) -> bool:
        return self.chat_type is ChatType.PRIVATE

    @property
    def dedup_key(self) -> tuple[uuid.UUID, int, int]:
        return (self.account_id, self.tg_chat_id, self.tg_message_id)

    def for_log(self) -> dict[str, Any]:
        """Поля для журнала: без текста сообщения."""
        return {
            "account_id": str(self.account_id),
            "chat_id": self.tg_chat_id,
            "message_id": self.tg_message_id,
            "sender_id": self.sender_tg_id,
            "chat_type": self.chat_type.value,
            "text_length": len(self.text),
        }


class MessageNormalizer:
    def __init__(self, max_text_length: int) -> None:
        self._max_text_length = max_text_length

    def normalize(self, account_id: uuid.UUID, event: Any) -> NormalizedMessage:
        message = getattr(event, "message", event)

        tg_message_id = _require_int(message, "id", "message id")
        tg_chat_id = _chat_id(event, message)
        outgoing = bool(getattr(message, "out", False))

        return NormalizedMessage(
            account_id=account_id,
            tg_chat_id=tg_chat_id,
            tg_message_id=tg_message_id,
            chat_type=detect_chat_type(event),
            text=self._text(message),
            date=_date(message),
            is_incoming=not outgoing,
            is_outgoing=outgoing,
            sender_tg_id=_optional_int(getattr(event, "sender_id", None))
            or _optional_int(getattr(message, "sender_id", None)),
            sender_username=_sender_attr(event, "username"),
            sender_display_name=_sender_display_name(event),
            chat_title=_chat_title(event),
            chat_username=_chat_username(event),
            reply_to_tg_message_id=_optional_int(getattr(message, "reply_to_msg_id", None)),
            is_forwarded=getattr(message, "fwd_from", None) is not None,
            media_type=detect_media_type(message),
        )

    def _text(self, message: Any) -> str:
        raw = getattr(message, "message", None)
        if not isinstance(raw, str):
            raw = getattr(message, "text", "") or ""
        text = str(raw).strip()
        # Обрезаем на входе: дальше по конвейеру текст попадает в промпт, и
        # ограничение должно быть одно и заданное настройкой.
        return text[: self._max_text_length]


def detect_chat_type(event: Any) -> ChatType:
    if getattr(event, "is_private", False):
        return ChatType.PRIVATE
    if getattr(event, "is_channel", False):
        # У Telegram супергруппа — тоже канал, отличается флагом megagroup.
        chat = getattr(event, "chat", None)
        if getattr(chat, "megagroup", False) or getattr(event, "is_group", False):
            return ChatType.SUPERGROUP
        return ChatType.CHANNEL
    if getattr(event, "is_group", False):
        return ChatType.GROUP
    return ChatType.GROUP


def detect_media_type(message: Any) -> MediaType | None:
    if getattr(message, "media", None) is None:
        return None
    for attribute, media_type in _MEDIA_ATTRIBUTES:
        if getattr(message, attribute, None) is not None:
            return media_type
    return MediaType.OTHER


def _chat_id(event: Any, message: Any) -> int:
    for source in (event, message):
        value = _optional_int(getattr(source, "chat_id", None))
        if value is not None:
            return value
    raise ValueError("message has no chat id")


def _date(message: Any) -> datetime:
    value = getattr(message, "date", None)
    if not isinstance(value, datetime):
        raise ValueError("message has no date")
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _sender_attr(event: Any, attribute: str) -> str | None:
    sender = getattr(event, "sender", None)
    value = getattr(sender, attribute, None)
    return str(value) if value else None


def _chat_title(event: Any) -> str | None:
    chat = getattr(event, "chat", None)
    title = getattr(chat, "title", None)
    return str(title) if title else None


def _chat_username(event: Any) -> str | None:
    chat = getattr(event, "chat", None)
    username = getattr(chat, "username", None)
    return str(username) if username else None


def _sender_display_name(event: Any) -> str | None:
    sender = getattr(event, "sender", None)
    if sender is None:
        return None
    title = getattr(sender, "title", None)
    if title:
        return str(title)
    parts = [getattr(sender, "first_name", None), getattr(sender, "last_name", None)]
    name = " ".join(str(part) for part in parts if part)
    return name or None


def _require_int(source: Any, attribute: str, label: str) -> int:
    value = _optional_int(getattr(source, attribute, None))
    if value is None:
        raise ValueError(f"message has no {label}")
    return value


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    return None
