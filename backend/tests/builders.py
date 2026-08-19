"""Конструкторы объектов для тестов.

Собраны отдельно, чтобы тесты читались как утверждения о поведении, а не как
длинные списки аргументов.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from app.models import ActionType, ChatType, MediaType, RuleScope
from app.rules.engine import CompiledRule, CooldownSpec
from app.rules.filters import MessageFilterSpec
from app.rules.keywords import KeywordSpec
from app.telegram.messages import NormalizedMessage

ACCOUNT_ID = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
CHAT_ID = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000001")


def message(
    text: str = "нужен дизайнер",
    *,
    account_id: uuid.UUID = ACCOUNT_ID,
    tg_chat_id: int = -1001234567890,
    tg_message_id: int = 555,
    chat_type: ChatType = ChatType.SUPERGROUP,
    sender_tg_id: int | None = 12345,
    is_outgoing: bool = False,
    is_forwarded: bool = False,
    media_type: MediaType | None = None,
    reply_to: int | None = None,
    date: datetime | None = None,
) -> NormalizedMessage:
    return NormalizedMessage(
        account_id=account_id,
        tg_chat_id=tg_chat_id,
        tg_message_id=tg_message_id,
        chat_type=chat_type,
        text=text,
        date=date or datetime(2026, 8, 18, 12, 0, tzinfo=UTC),
        is_incoming=not is_outgoing,
        is_outgoing=is_outgoing,
        sender_tg_id=sender_tg_id,
        sender_username="client",
        sender_display_name="Клиент",
        reply_to_tg_message_id=reply_to,
        is_forwarded=is_forwarded,
        media_type=media_type,
    )


def rule(
    name: str = "Поиск клиентов",
    *,
    priority: int = 100,
    stop_on_match: bool = True,
    scope: RuleScope = RuleScope.CHAT_MONITOR,
    terms: tuple[str, ...] = ("нужен дизайнер",),
    exclude: tuple[str, ...] = (),
    regex: str | None = None,
    filters: MessageFilterSpec | None = None,
    ai_enabled: bool = False,
    ai_threshold: float | None = None,
    action: ActionType = ActionType.REPLY,
    account_ids: frozenset[uuid.UUID] = frozenset(),
    chat_ids: frozenset[uuid.UUID] = frozenset(),
    cooldown: CooldownSpec | None = None,
    scenario_id: uuid.UUID | None = None,
) -> CompiledRule:
    return CompiledRule(
        id=uuid.uuid4(),
        name=name,
        priority=priority,
        stop_on_match=stop_on_match,
        scope=scope,
        scenario_id=scenario_id,
        action=action,
        action_config={},
        filters=filters or MessageFilterSpec(),
        keywords=KeywordSpec(terms=terms, exclude=exclude),
        regex=regex,
        ai_enabled=ai_enabled,
        ai_threshold=ai_threshold,
        cooldown=cooldown or CooldownSpec(),
        account_ids=account_ids,
        chat_ids=chat_ids,
    )


class FakeEvent:
    """Событие Telethon в объёме, который читает нормализатор."""

    def __init__(
        self,
        *,
        message_id: int = 555,
        chat_id: int = -1001234567890,
        text: str = "нужен дизайнер",
        out: bool = False,
        is_private: bool = False,
        is_group: bool = True,
        is_channel: bool = False,
        megagroup: bool = False,
        date: datetime | None = None,
        sender_id: int | None = 12345,
        reply_to_msg_id: int | None = None,
        fwd_from: object | None = None,
        media: object | None = None,
        media_kind: str | None = None,
        first_name: str | None = "Клиент",
        last_name: str | None = None,
        username: str | None = "client",
    ) -> None:
        self.chat_id = chat_id
        self.is_private = is_private
        self.is_group = is_group
        self.is_channel = is_channel
        self.chat = type("Chat", (), {"megagroup": megagroup})()
        self.sender_id = sender_id
        self.sender = type(
            "Sender",
            (),
            {"username": username, "first_name": first_name, "last_name": last_name},
        )()
        self.message = _FakeMessage(
            message_id=message_id,
            chat_id=chat_id,
            text=text,
            out=out,
            date=date or datetime(2026, 8, 18, 12, 0, tzinfo=UTC),
            reply_to_msg_id=reply_to_msg_id,
            fwd_from=fwd_from,
            media=media,
            media_kind=media_kind,
        )


class _FakeMessage:
    def __init__(
        self,
        *,
        message_id: int,
        chat_id: int,
        text: str,
        out: bool,
        date: datetime,
        reply_to_msg_id: int | None,
        fwd_from: object | None,
        media: object | None,
        media_kind: str | None,
    ) -> None:
        self.id = message_id
        self.chat_id = chat_id
        self.message = text
        self.out = out
        self.date = date
        self.reply_to_msg_id = reply_to_msg_id
        self.fwd_from = fwd_from
        self.media = media
        if media_kind is not None:
            self.media = media or object()
            setattr(self, media_kind, object())

    def __getattr__(self, item: str) -> Any:
        # Telethon-сообщение отвечает None на все неустановленные виды медиа.
        if item.startswith("_"):
            raise AttributeError(item)
        return None
