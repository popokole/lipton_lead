"""Шина команд API → воркер.

Telethon-клиент живёт только в воркере: два подключения к одной сессии
приводят к разлогину аккаунта. Поэтому всё, что требует живого клиента —
авторизация, список диалогов, отправка сообщения, — API выполняет не сам, а
просит владельца аккаунта.

Транспорт: Redis Stream на воркер (команда) + список с TTL на ответ. Стрим,
а не pub/sub, потому что команда не должна потеряться, если воркер на
миллисекунду отстал от подписки.

Отдельно про авторизацию: `phone_code_hash` привязан к тому соединению,
которое запрашивало код. Поэтому SEND_CODE и SIGN_IN обязаны попасть в один
и тот же процесс — маршрутизация по владельцу аренды это и обеспечивает.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from redis.asyncio import Redis

from app.bus import keys
from app.bus.messages import Command, CommandResult
from app.core.errors import CommandTimeoutError, WorkerUnavailableError
from app.core.logging import get_logger
from app.workers.lease import AccountLease

logger = get_logger(__name__)

REPLY_TTL_SECONDS = 120
STREAM_MAXLEN = 10_000


class CommandBus:
    """Клиентская сторона: используется API."""

    def __init__(self, redis: Redis, lease: AccountLease) -> None:
        self._redis = redis
        self._lease = lease

    async def call(self, command: Command, *, timeout_seconds: float = 30.0) -> CommandResult:
        worker_id = await self._lease.holder(command.account_id)
        if worker_id is None:
            raise WorkerUnavailableError(
                "Account is not attached to any worker yet",
                account_id=str(command.account_id),
            )

        reply_key = keys.command_reply(command.id)
        await self._redis.xadd(
            keys.command_stream(worker_id),
            {"payload": command.model_dump_json()},
            maxlen=STREAM_MAXLEN,
            approximate=True,
        )
        logger.info("command_sent", worker_id=worker_id, **command.redacted())

        reply = await self._redis.blpop([reply_key], timeout=int(timeout_seconds))
        if reply is None:
            raise CommandTimeoutError(
                f"Worker {worker_id} did not answer {command.type} in {timeout_seconds:.0f}s"
            )

        _, raw = reply
        return CommandResult.model_validate_json(raw)


class CommandConsumer:
    """Серверная сторона: используется воркером."""

    def __init__(self, redis: Redis, worker_id: str) -> None:
        self._redis = redis
        self._worker_id = worker_id
        self._stream = keys.command_stream(worker_id)
        self._last_id = "$"

    async def listen(self, *, block_ms: int = 1000) -> AsyncIterator[Command]:
        """Бесконечно читает свой стрим. Битые сообщения пропускает, не падая."""
        while True:
            try:
                entries = await self._redis.xread(
                    {self._stream: self._last_id}, count=16, block=block_ms
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — Redis мог моргнуть
                logger.warning("command_stream_read_failed", detail=str(exc))
                await asyncio.sleep(1.0)
                continue

            for _stream, records in entries or []:
                for entry_id, fields in records:
                    self._last_id = entry_id
                    try:
                        yield Command.model_validate_json(fields["payload"])
                    except (KeyError, ValueError) as exc:
                        logger.warning("command_unparsable", entry_id=entry_id, detail=str(exc))

    async def reply(self, result: CommandResult) -> None:
        reply_key = keys.command_reply(result.command_id)
        pipe = self._redis.pipeline()
        pipe.rpush(reply_key, result.model_dump_json())
        pipe.expire(reply_key, REPLY_TTL_SECONDS)
        await pipe.execute()

    async def drop_stream(self) -> None:
        """Убирает свой стрим при остановке: адресат исчез вместе с воркером."""
        await self._redis.delete(self._stream)
