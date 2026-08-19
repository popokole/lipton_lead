"""Аренда аккаунта: гарантия «один аккаунт — один воркер».

Это самая дорогая ошибка в системе: два воркера на одной сессии приводят не к
дублю сообщения, а к разлогину аккаунта в Telegram. Поэтому проверяется не
только счастливый путь, но и все попытки перехватить чужую аренду.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from redis.asyncio import Redis

from app.workers.lease import AccountLease

pytestmark = pytest.mark.integration


async def test_only_the_first_worker_gets_the_account(redis_client: Redis) -> None:
    account_id = uuid.uuid4()
    first = AccountLease(redis_client, "worker-1", ttl_seconds=30)
    second = AccountLease(redis_client, "worker-2", ttl_seconds=30)

    assert await first.try_acquire(account_id) is True
    assert await second.try_acquire(account_id) is False
    assert await first.holder(account_id) == "worker-1"


async def test_holder_returns_none_for_free_account(redis_client: Redis) -> None:
    lease = AccountLease(redis_client, "worker-1", ttl_seconds=30)
    assert await lease.holder(uuid.uuid4()) is None


async def test_owner_can_renew(redis_client: Redis) -> None:
    account_id = uuid.uuid4()
    lease = AccountLease(redis_client, "worker-1", ttl_seconds=30)

    await lease.try_acquire(account_id)
    assert await lease.renew(account_id) is True


async def test_stranger_cannot_renew_someone_elses_lease(redis_client: Redis) -> None:
    account_id = uuid.uuid4()
    owner = AccountLease(redis_client, "worker-1", ttl_seconds=30)
    stranger = AccountLease(redis_client, "worker-2", ttl_seconds=30)

    await owner.try_acquire(account_id)

    assert await stranger.renew(account_id) is False
    assert await owner.holder(account_id) == "worker-1"


async def test_stranger_cannot_release_someone_elses_lease(redis_client: Redis) -> None:
    account_id = uuid.uuid4()
    owner = AccountLease(redis_client, "worker-1", ttl_seconds=30)
    stranger = AccountLease(redis_client, "worker-2", ttl_seconds=30)

    await owner.try_acquire(account_id)
    await stranger.release(account_id)

    assert await owner.holder(account_id) == "worker-1", "чужая аренда должна остаться"


async def test_release_frees_the_account_for_others(redis_client: Redis) -> None:
    account_id = uuid.uuid4()
    first = AccountLease(redis_client, "worker-1", ttl_seconds=30)
    second = AccountLease(redis_client, "worker-2", ttl_seconds=30)

    await first.try_acquire(account_id)
    await first.release(account_id)

    assert await second.try_acquire(account_id) is True
    assert await second.holder(account_id) == "worker-2"


async def test_expired_lease_is_picked_up_by_another_worker(redis_client: Redis) -> None:
    """Упавший воркер не блокирует аккаунт навсегда: аренда живёт по TTL."""
    account_id = uuid.uuid4()
    dead = AccountLease(redis_client, "worker-dead", ttl_seconds=5)
    alive = AccountLease(redis_client, "worker-alive", ttl_seconds=5)

    await dead.try_acquire(account_id)
    # Имитируем истечение TTL, не ожидая его в реальном времени.
    await redis_client.delete(f"tgai:lease:account:{account_id}")

    assert await alive.try_acquire(account_id) is True
    assert await dead.renew(account_id) is False


async def test_renewal_after_losing_the_lease_fails(redis_client: Redis) -> None:
    account_id = uuid.uuid4()
    dead = AccountLease(redis_client, "worker-dead", ttl_seconds=5)
    alive = AccountLease(redis_client, "worker-alive", ttl_seconds=5)

    await dead.try_acquire(account_id)
    await redis_client.delete(f"tgai:lease:account:{account_id}")
    await alive.try_acquire(account_id)

    assert await dead.renew(account_id) is False
    assert await alive.renew(account_id) is True


async def test_concurrent_acquire_has_exactly_one_winner(redis_client: Redis) -> None:
    account_id = uuid.uuid4()
    leases = [AccountLease(redis_client, f"worker-{i}", ttl_seconds=30) for i in range(20)]

    results = await asyncio.gather(*(lease.try_acquire(account_id) for lease in leases))

    assert sum(results) == 1, "аккаунт не может достаться двум воркерам одновременно"
