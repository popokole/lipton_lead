"""Проверка health-эндпоинтов на живых PostgreSQL и Redis."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.bus import keys
from app.core.clock import utcnow
from app.core.config import Settings
from app.core.runtime import VERSION
from app.main import create_app
from app.workers.registry import STATUS_HEALTHY, WorkerRegistry, build_heartbeat

pytestmark = pytest.mark.integration


@pytest.fixture
async def app(integration_settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(integration_settings)
    async with LifespanManager(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.fixture
async def registry(app: FastAPI) -> AsyncIterator[WorkerRegistry]:
    """Чистый реестр воркеров: тесты не должны видеть чужие heartbeat-ы."""
    worker_registry: WorkerRegistry = app.state.runtime.worker_registry
    for heartbeat in await worker_registry.list_alive():
        await worker_registry.unregister(heartbeat.worker_id)
    yield worker_registry


async def test_live_does_not_touch_dependencies(client: AsyncClient) -> None:
    response = await client.get("/live")
    assert response.status_code == 200
    assert response.json() == {"status": "alive", "version": VERSION}


async def test_health_reports_storage_up(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["components"]["postgres"]["state"] == "up"
    assert body["components"]["redis"]["state"] == "up"


async def test_ready_returns_200_while_storage_is_up(client: AsyncClient) -> None:
    response = await client.get("/ready")
    assert response.status_code == 200
    assert response.json()["status"] in {"ok", "degraded"}


async def test_health_is_degraded_without_workers(
    client: AsyncClient, registry: WorkerRegistry
) -> None:
    body = (await client.get("/health")).json()
    assert body["status"] == "degraded"
    assert body["workers_healthy"] == 0
    assert body["components"]["workers"]["state"] == "down"


async def test_health_is_ok_with_a_healthy_worker(
    client: AsyncClient, registry: WorkerRegistry
) -> None:
    heartbeat = build_heartbeat(
        worker_id="00000000-0000-0000-0000-0000000000ff",
        name="test-worker",
        hostname="test",
        pid=1,
        version=VERSION,
        started_at=utcnow(),
        status=STATUS_HEALTHY,
    )
    await registry.publish(heartbeat)
    try:
        body = (await client.get("/health")).json()
        assert body["status"] == "ok"
        assert body["workers_healthy"] >= 1
    finally:
        await registry.unregister(heartbeat.worker_id)


async def test_expired_heartbeat_disappears_from_registry(registry: WorkerRegistry) -> None:
    heartbeat = build_heartbeat(
        worker_id="00000000-0000-0000-0000-0000000000fe",
        name="ghost-worker",
        hostname="test",
        pid=2,
        version=VERSION,
        started_at=utcnow(),
        status=STATUS_HEALTHY,
    )
    await registry.publish(heartbeat)
    # Имитируем упавший воркер: запись heartbeat исчезла, индекс остался грязным.
    # Лезем во внутренний клиент намеренно: проверяем именно ленивую чистку индекса.
    await registry._redis.delete(keys.worker_heartbeat(heartbeat.worker_id))
    assert await registry.list_alive() == []


async def test_metrics_exposed(client: AsyncClient) -> None:
    await client.get("/health")
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "http_request" in response.text
