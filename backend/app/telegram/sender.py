"""Отправка сообщений от имени аккаунта.

Отправка сериализована на аккаунт: два ответа не уходят одновременно и между
ними выдерживается минимальный интервал. Это защита самого аккаунта от
FloodWait, а не способ обойти ограничения Telegram — при FloodWaitError мы
честно ждём столько, сколько попросил сервер, и ничего не переключаем.

Слишком длинную паузу (больше FLOOD_WAIT_MAX_SECONDS) держать в задаче
бессмысленно: такое ожидание — сигнал, что аккаунт нагружен неправильно, и
его надо показать оператору, а не пересидеть молча.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from dataclasses import dataclass
from typing import Any

from app.core.clock import get_clock
from app.core.config import Settings
from app.core.errors import TelegramError, TelegramFloodWaitError
from app.core.logging import get_logger
from app.telegram.client import TelegramClientLike

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SentMessage:
    tg_message_id: int
    chat_id: int


class MessageSender:
    """Последовательная отправка с соблюдением интервала."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._locks: dict[uuid.UUID, asyncio.Lock] = {}
        self._last_sent: dict[uuid.UUID, float] = {}

    def forget(self, account_id: uuid.UUID) -> None:
        self._locks.pop(account_id, None)
        self._last_sent.pop(account_id, None)

    async def send(
        self,
        account_id: uuid.UUID,
        client: TelegramClientLike,
        *,
        chat_id: int,
        text: str,
        reply_to: int | None = None,
        peer: Any | None = None,
    ) -> SentMessage:
        lock = self._locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            await self._respect_interval(account_id)
            target: Any = peer if peer is not None else chat_id
            await self._simulate_human_delay(account_id, client, target, reply_to)
            return await self._send_once(account_id, client, chat_id, text, reply_to, peer)

    async def _simulate_human_delay(
        self,
        account_id: uuid.UUID,
        client: TelegramClientLike,
        target: Any,
        reply_to: int | None,
    ) -> None:
        """Отмечает сообщение прочитанным и «печатает» несколько секунд.

        Мгновенный ответ на длинное сообщение выдаёт бота вернее, чем что
        угодно другое. Имитация необязательна для доставки ответа: сбой
        здесь не должен срывать саму отправку.
        """
        high = self._settings.reply_typing_delay_max_seconds
        if high <= 0:
            return
        low = min(self._settings.reply_typing_delay_min_seconds, high)
        delay = random.uniform(low, high)

        try:
            if reply_to is not None:
                # max_id, не message: Telethon у голого int читает `.id`,
                # а не трактует его как id сообщения — с message=<int> здесь
                # падает AttributeError на каждом вызове.
                await client.send_read_acknowledge(target, max_id=reply_to)
            async with client.action(target, "typing"):
                await asyncio.sleep(delay)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "human_delay_simulation_failed", account_id=str(account_id), detail=str(exc)
            )

    async def _respect_interval(self, account_id: uuid.UUID) -> None:
        interval = self._settings.send_min_interval_seconds
        if interval <= 0:
            return
        clock = get_clock()
        last = self._last_sent.get(account_id)
        if last is None:
            return
        elapsed = clock.monotonic() - last
        if elapsed < interval:
            await asyncio.sleep(interval - elapsed)

    async def _send_once(
        self,
        account_id: uuid.UUID,
        client: TelegramClientLike,
        chat_id: int,
        text: str,
        reply_to: int | None,
        peer: Any | None,
    ) -> SentMessage:
        from telethon.errors import FloodWaitError, RPCError

        # peer несёт access_hash и потому надёжнее числового id: сессия из
        # tdata приходит без кеша сущностей, и по одному id Telethon чат не
        # найдёт.
        target: Any = peer if peer is not None else chat_id

        for attempt in (1, 2):
            try:
                sent = await client.send_message(target, text, reply_to=reply_to)
            except FloodWaitError as exc:
                if attempt == 2 or exc.seconds > self._settings.flood_wait_max_seconds:
                    logger.warning(
                        "flood_wait_exceeded",
                        account_id=str(account_id),
                        seconds=exc.seconds,
                    )
                    raise TelegramFloodWaitError(exc.seconds) from exc
                logger.info("flood_wait", account_id=str(account_id), seconds=exc.seconds)
                await asyncio.sleep(exc.seconds + 1)
                continue
            except RPCError as exc:
                raise TelegramError(f"{type(exc).__name__}: {exc}") from exc

            self._last_sent[account_id] = get_clock().monotonic()
            return SentMessage(tg_message_id=_message_id(sent), chat_id=chat_id)

        raise TelegramError("send retries exhausted")


def _message_id(sent: Any) -> int:
    message_id = getattr(sent, "id", None)
    if not isinstance(message_id, int):
        raise TelegramError("Telegram returned a message without an id")
    return message_id
