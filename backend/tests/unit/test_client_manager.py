"""Поведение ClientManager: изоляция аккаунтов и переподключение (ТЗ §5)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable

import pytest

from app.models import AccountStatus
from app.telegram.client_manager import ClientManager
from app.telegram.session_manager import SessionCredentials
from tests.conftest import make_settings
from tests.fakes import FakeClientFactory, FakeTelegramClient, make_session_string

CREDENTIALS = SessionCredentials(make_session_string(), 12345, "hash")


def build(factory: FakeClientFactory, **overrides: object) -> tuple[ClientManager, list[tuple]]:
    """Собирает менеджер с быстрыми задержками и журналом статусов."""
    settings = make_settings(
        telegram_reconnect_base_delay=0.01,
        telegram_reconnect_max_delay=0.05,
        telegram_connect_timeout=1.0,
        **overrides,
    )
    statuses: list[tuple] = []

    async def on_status(account_id: uuid.UUID, status: AccountStatus, error: str | None) -> None:
        statuses.append((account_id, status, error))

    return ClientManager(settings, factory, on_status=on_status), statuses


async def test_authorised_account_goes_online() -> None:
    account_id = uuid.uuid4()
    factory = FakeClientFactory(FakeTelegramClient())
    manager, statuses = build(factory)

    await manager.start(account_id, CREDENTIALS)
    assert await manager.wait_ready(account_id, timeout_seconds=2.0) is True

    health = manager.health(account_id)
    assert health is not None
    assert health.status is AccountStatus.ONLINE
    assert health.connected is True
    assert health.authorized is True
    assert (account_id, AccountStatus.ONLINE, None) in statuses

    await manager.shutdown()


async def test_unauthorised_session_stops_retrying() -> None:
    """Слетевшая сессия — не сетевая ошибка: повторять бессмысленно."""
    account_id = uuid.uuid4()
    client = FakeTelegramClient(authorized=False)
    factory = FakeClientFactory(client)
    manager, statuses = build(factory)

    await manager.start(account_id, CREDENTIALS)
    assert await manager.wait_ready(account_id, timeout_seconds=2.0) is False

    await asyncio.sleep(0.2)
    health = manager.health(account_id)
    assert health is not None
    assert health.status is AccountStatus.AUTH_REQUIRED
    assert client.connect_calls == 1, "повторных попыток быть не должно"
    assert any(status is AccountStatus.AUTH_REQUIRED for _, status, _ in statuses)

    await manager.shutdown()


async def test_transient_failure_is_retried() -> None:
    account_id = uuid.uuid4()
    client = FakeTelegramClient(connect_failures=2)
    factory = FakeClientFactory(client)
    manager, _ = build(factory)

    await manager.start(account_id, CREDENTIALS)
    assert await manager.wait_ready(account_id, timeout_seconds=3.0) is False

    for _ in range(100):
        health = manager.health(account_id)
        if health is not None and health.status is AccountStatus.ONLINE:
            break
        await asyncio.sleep(0.05)

    health = manager.health(account_id)
    assert health is not None
    assert health.status is AccountStatus.ONLINE
    assert client.connect_calls == 3

    await manager.shutdown()


async def test_one_account_failure_does_not_affect_another() -> None:
    """Ключевое требование ТЗ §45: ошибки аккаунтов изолированы."""
    broken_id, healthy_id = uuid.uuid4(), uuid.uuid4()
    broken = FakeTelegramClient(authorized=False)
    healthy = FakeTelegramClient()

    factory = FakeClientFactory()
    factory.preset(broken_id, broken)
    factory.preset(healthy_id, healthy)
    manager, _ = build(factory)

    await manager.start(broken_id, CREDENTIALS)
    await manager.start(healthy_id, CREDENTIALS)

    assert await manager.wait_ready(healthy_id, timeout_seconds=2.0) is True
    broken_health = manager.health(broken_id)
    healthy_health = manager.health(healthy_id)
    assert broken_health is not None and broken_health.status is AccountStatus.AUTH_REQUIRED
    assert healthy_health is not None and healthy_health.status is AccountStatus.ONLINE

    await manager.shutdown()


async def test_stop_disconnects_and_forgets_account() -> None:
    account_id = uuid.uuid4()
    client = FakeTelegramClient()
    manager, _ = build(FakeClientFactory(client))

    await manager.start(account_id, CREDENTIALS)
    await manager.wait_ready(account_id, timeout_seconds=2.0)
    await manager.stop(account_id)

    assert client.disconnect_calls == 1
    assert client.logged_out is False
    assert manager.get(account_id) is None
    assert manager.account_ids == []


async def test_stop_with_logout_revokes_session_in_telegram() -> None:
    account_id = uuid.uuid4()
    client = FakeTelegramClient()
    manager, _ = build(FakeClientFactory(client))

    await manager.start(account_id, CREDENTIALS)
    await manager.wait_ready(account_id, timeout_seconds=2.0)
    await manager.stop(account_id, logout=True)

    assert client.logged_out is True
    assert client.disconnect_calls == 1


async def test_starting_same_account_twice_is_idempotent() -> None:
    account_id = uuid.uuid4()
    client = FakeTelegramClient()
    manager, _ = build(FakeClientFactory(client))

    await manager.start(account_id, CREDENTIALS)
    await manager.start(account_id, CREDENTIALS)
    await manager.wait_ready(account_id, timeout_seconds=2.0)

    assert manager.account_ids == [account_id]
    assert client.connect_calls == 1

    await manager.shutdown()


async def test_only_incoming_messages_are_handled() -> None:
    """Собственные сообщения не должны попадать в конвейер (ТЗ §9)."""
    account_id = uuid.uuid4()
    client = FakeTelegramClient()
    settings = make_settings(telegram_reconnect_base_delay=0.01, telegram_reconnect_max_delay=0.05)
    seen: list[uuid.UUID] = []

    def factory_for(account: uuid.UUID) -> Callable[[object], Awaitable[None]]:
        async def handler(event: object) -> None:
            seen.append(account)

        return handler

    manager = ClientManager(settings, FakeClientFactory(client), handler_factory=factory_for)
    await manager.start(account_id, CREDENTIALS)
    await manager.wait_ready(account_id, timeout_seconds=2.0)

    assert len(client.handlers) == 1
    _callback, event_filter = client.handlers[0]
    assert event_filter.incoming is True
    assert event_filter.outgoing is False

    await manager.shutdown()


@pytest.mark.parametrize("logout", [False, True])
async def test_stopping_unknown_account_is_safe(logout: bool) -> None:
    manager, _ = build(FakeClientFactory())
    await manager.stop(uuid.uuid4(), logout=logout)
