"""Health-эндпоинты (ТЗ §36).

Разделение намеренное:

* ``/health`` — диагностика. Всегда 200, если процесс жив. Перезапуск API не
  чинит упавший PostgreSQL, поэтому падение зависимости не должно приводить к
  тому, что Docker будет бесконечно убивать контейнер.
* ``/ready`` — готовность обслуживать трафик. 503, если PostgreSQL или Redis
  недоступны: балансировщику нужно перестать слать запросы.

Отсутствие живых воркеров — это ``degraded``, а не ``down``: панель продолжает
работать и обязана показать, что обработка сообщений остановлена.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, Field

from app.api.deps import RuntimeDep
from app.core.runtime import VERSION, Runtime

router = APIRouter(tags=["health"])

ComponentState = Literal["up", "down"]
OverallState = Literal["ok", "degraded", "down"]


class ComponentHealth(BaseModel):
    state: ComponentState
    detail: str | None = None


class HealthResponse(BaseModel):
    status: OverallState
    version: str
    environment: str
    components: dict[str, ComponentHealth]
    workers_alive: int = Field(ge=0)
    workers_healthy: int = Field(ge=0)


async def _collect(runtime: Runtime) -> HealthResponse:
    db_ok, redis_ok = await asyncio.gather(runtime.database.ping(), runtime.redis.ping())

    workers_alive = 0
    workers_healthy = 0
    if redis_ok:
        heartbeats = await runtime.worker_registry.list_alive()
        workers_alive = len(heartbeats)
        workers_healthy = sum(1 for hb in heartbeats if hb.status == "HEALTHY")

    components = {
        "postgres": ComponentHealth(state="up" if db_ok else "down"),
        "redis": ComponentHealth(state="up" if redis_ok else "down"),
        "workers": ComponentHealth(
            state="up" if workers_healthy else "down",
            detail=None if workers_healthy else "no healthy worker is reporting",
        ),
    }

    if not (db_ok and redis_ok):
        overall: OverallState = "down"
    elif not workers_healthy:
        overall = "degraded"
    else:
        overall = "ok"

    return HealthResponse(
        status=overall,
        version=VERSION,
        environment=runtime.settings.env,
        components=components,
        workers_alive=workers_alive,
        workers_healthy=workers_healthy,
    )


@router.get("/health", response_model=HealthResponse, summary="Диагностика компонентов")
async def health(runtime: RuntimeDep) -> HealthResponse:
    return await _collect(runtime)


@router.get("/ready", response_model=HealthResponse, summary="Готовность принимать трафик")
async def ready(runtime: RuntimeDep, response: Response) -> HealthResponse:
    report = await _collect(runtime)
    if report.status == "down":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return report


@router.get("/live", include_in_schema=False)
async def live() -> dict[str, str]:
    """Liveness без обращения к зависимостям — для Docker healthcheck."""
    return {"status": "alive", "version": VERSION}
