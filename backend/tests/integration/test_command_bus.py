"""Шина команд API → воркер."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis

from app.bus.commands import CommandBus, CommandConsumer
from app.bus.messages import Command, CommandResult, CommandType
from app.core.errors import CommandTimeoutError, WorkerUnavailableError
from app.workers.lease import AccountLease

pytestmark = pytest.mark.integration

WORKER_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
async def leased_account(redis_client: Redis) -> AsyncIterator[uuid.UUID]:
    """Аккаунт, арендованный воркером WORKER_ID."""
    account_id = uuid.uuid4()
    lease = AccountLease(redis_client, WORKER_ID, ttl_seconds=60)
    await lease.try_acquire(account_id)
    yield account_id
    await lease.release(account_id)


async def _serve_once(consumer: CommandConsumer, reply: CommandResult | None = None) -> Command:
    """Читает одну команду и отвечает на неё."""
    async for command in consumer.listen(block_ms=100):
        answer = reply or CommandResult.success(command.id, pong=True)
        await consumer.reply(answer.model_copy(update={"command_id": command.id}))
        return command
    raise AssertionError("consumer stopped without receiving a command")


async def test_command_reaches_the_owning_worker(
    redis_client: Redis, leased_account: uuid.UUID
) -> None:
    consumer = CommandConsumer(redis_client, WORKER_ID)
    # Подписка до отправки: стрим читается с "$", то есть только новые записи.
    server = asyncio.create_task(_serve_once(consumer))
    await asyncio.sleep(0.1)

    bus = CommandBus(redis_client, AccountLease(redis_client, "api", ttl_seconds=60))
    result = await bus.call(
        Command(type=CommandType.PING, account_id=leased_account), timeout_seconds=5
    )

    received = await server
    assert received.type is CommandType.PING
    assert received.account_id == leased_account
    assert result.ok is True
    assert result.data["pong"] is True


async def test_call_for_unattached_account_fails_fast(redis_client: Redis) -> None:
    bus = CommandBus(redis_client, AccountLease(redis_client, "api", ttl_seconds=60))

    with pytest.raises(WorkerUnavailableError):
        await bus.call(Command(type=CommandType.PING, account_id=uuid.uuid4()), timeout_seconds=1)


async def test_silent_worker_produces_timeout(
    redis_client: Redis, leased_account: uuid.UUID
) -> None:
    bus = CommandBus(redis_client, AccountLease(redis_client, "api", ttl_seconds=60))

    with pytest.raises(CommandTimeoutError):
        await bus.call(Command(type=CommandType.PING, account_id=leased_account), timeout_seconds=1)


async def test_failure_result_is_delivered_as_data_not_exception(
    redis_client: Redis, leased_account: uuid.UUID
) -> None:
    consumer = CommandConsumer(redis_client, WORKER_ID)
    failure = CommandResult.failure(uuid.uuid4(), "invalid_input", "Неверный код")
    server = asyncio.create_task(_serve_once(consumer, failure))
    await asyncio.sleep(0.1)

    bus = CommandBus(redis_client, AccountLease(redis_client, "api", ttl_seconds=60))
    result = await bus.call(
        Command(type=CommandType.SIGN_IN, account_id=leased_account, payload={"code": "1"}),
        timeout_seconds=5,
    )
    await server

    assert result.ok is False
    assert result.error_code == "invalid_input"
    assert result.error_message == "Неверный код"


async def test_secrets_never_appear_in_log_view() -> None:
    """`redacted()` — то, что уходит в лог: только тип и адресат."""
    command = Command(
        type=CommandType.SIGN_IN_PASSWORD,
        account_id=uuid.uuid4(),
        payload={"password": "very-secret", "code": "12345"},
    )

    view = command.redacted()

    assert "very-secret" not in str(view)
    assert "12345" not in str(view)
    assert view["type"] is CommandType.SIGN_IN_PASSWORD


async def test_malformed_entry_does_not_stop_the_consumer(
    redis_client: Redis, leased_account: uuid.UUID
) -> None:
    consumer = CommandConsumer(redis_client, WORKER_ID)
    server = asyncio.create_task(_serve_once(consumer))
    await asyncio.sleep(0.1)

    await redis_client.xadd(f"tgai:commands:{WORKER_ID}", {"payload": "{not json"})
    bus = CommandBus(redis_client, AccountLease(redis_client, "api", ttl_seconds=60))
    result = await bus.call(
        Command(type=CommandType.PING, account_id=leased_account), timeout_seconds=5
    )
    await server

    assert result.ok is True
