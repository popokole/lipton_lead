"""Отправка сообщений: интервал и FloodWait (ТЗ §20, §38)."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.core.errors import TelegramFloodWaitError
from app.telegram.sender import MessageSender
from tests.conftest import make_settings
from tests.fakes import FakeTelegramClient

ACCOUNT = uuid.uuid4()
CHAT = 777


async def test_message_reaches_telegram() -> None:
    sender = MessageSender(make_settings(send_min_interval_seconds=0))
    client = FakeTelegramClient()

    sent = await sender.send(ACCOUNT, client, chat_id=CHAT, text="привет", reply_to=42)

    assert client.sent == [(CHAT, "привет", 42)]
    assert sent.chat_id == CHAT
    assert sent.tg_message_id > 0


async def test_minimum_interval_is_respected() -> None:
    """Интервал защищает аккаунт от FloodWait, а не обходит лимиты Telegram."""
    sender = MessageSender(make_settings(send_min_interval_seconds=0.2))
    client = FakeTelegramClient()

    started = asyncio.get_running_loop().time()
    await sender.send(ACCOUNT, client, chat_id=CHAT, text="раз")
    await sender.send(ACCOUNT, client, chat_id=CHAT, text="два")
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed >= 0.2
    assert len(client.sent) == 2


async def test_sends_are_serialised_per_account() -> None:
    sender = MessageSender(make_settings(send_min_interval_seconds=0))
    client = FakeTelegramClient()

    await asyncio.gather(
        *(sender.send(ACCOUNT, client, chat_id=CHAT, text=str(i)) for i in range(5))
    )

    assert [text for _chat, text, _reply in client.sent] == ["0", "1", "2", "3", "4"]


async def test_short_flood_wait_is_waited_out() -> None:
    sender = MessageSender(make_settings(send_min_interval_seconds=0, flood_wait_max_seconds=600))
    client = FakeTelegramClient(flood_wait_seconds=0)

    sent = await sender.send(ACCOUNT, client, chat_id=CHAT, text="после паузы")

    assert sent.tg_message_id > 0
    assert client.sent == [(CHAT, "после паузы", None)]


async def test_long_flood_wait_is_surfaced_not_slept_through() -> None:
    sender = MessageSender(make_settings(send_min_interval_seconds=0, flood_wait_max_seconds=5))
    client = FakeTelegramClient(flood_wait_seconds=3600)

    with pytest.raises(TelegramFloodWaitError) as exc_info:
        await sender.send(ACCOUNT, client, chat_id=CHAT, text="не уйдёт")

    assert exc_info.value.seconds == 3600
    assert client.sent == []


async def test_interval_is_tracked_per_account() -> None:
    sender = MessageSender(make_settings(send_min_interval_seconds=5))
    client = FakeTelegramClient()
    first, second = uuid.uuid4(), uuid.uuid4()

    started = asyncio.get_running_loop().time()
    await sender.send(first, client, chat_id=CHAT, text="a")
    await sender.send(second, client, chat_id=CHAT, text="b")
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 1.0, "пауза одного аккаунта не должна задерживать другой"


async def test_forget_clears_account_state() -> None:
    sender = MessageSender(make_settings(send_min_interval_seconds=5))
    client = FakeTelegramClient()

    await sender.send(ACCOUNT, client, chat_id=CHAT, text="a")
    sender.forget(ACCOUNT)

    started = asyncio.get_running_loop().time()
    await sender.send(ACCOUNT, client, chat_id=CHAT, text="b")
    assert asyncio.get_running_loop().time() - started < 1.0
