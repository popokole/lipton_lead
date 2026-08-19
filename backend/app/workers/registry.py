"""Реестр живых воркеров в Redis.

Воркер публикует heartbeat с TTL; API читает реестр, чтобы показать статус в
панели и ответить на /health. Redis выбран специально: запись раз в несколько
секунд от каждого воркера не должна создавать нагрузку на PostgreSQL.
Долговременное состояние воркера (таблица `workers`) появится на этапе 2.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from redis.asyncio import Redis

from app.bus import keys
from app.core.clock import utcnow
from app.core.logging import get_logger

logger = get_logger(__name__)

STATUS_STARTING = "STARTING"
STATUS_HEALTHY = "HEALTHY"
STATUS_DEGRADED = "DEGRADED"
STATUS_ERROR = "ERROR"
STATUS_STOPPED = "STOPPED"


@dataclass(slots=True)
class WorkerHeartbeat:
    worker_id: str
    name: str
    status: str
    hostname: str
    pid: int
    version: str
    started_at: str
    updated_at: str
    accounts: list[str] = field(default_factory=list)
    queue_size: int = 0
    last_error: str | None = None
    last_processed_at: str | None = None

    @property
    def accounts_count(self) -> int:
        return len(self.accounts)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> WorkerHeartbeat:
        data: dict[str, Any] = json.loads(raw)
        return cls(**data)


class WorkerRegistry:
    """Публикация и чтение heartbeat-ов.

    Запись heartbeat идёт с TTL: упавший воркер исчезает из реестра сам, без
    сборщика мусора. Индекс в SET чистится лениво при чтении.
    """

    def __init__(self, redis: Redis, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    async def publish(self, heartbeat: WorkerHeartbeat) -> None:
        pipe = self._redis.pipeline()
        pipe.set(keys.worker_heartbeat(heartbeat.worker_id), heartbeat.to_json(), ex=self._ttl)
        pipe.sadd(keys.WORKER_INDEX, heartbeat.worker_id)
        await pipe.execute()

    async def unregister(self, worker_id: UUID | str) -> None:
        pipe = self._redis.pipeline()
        pipe.delete(keys.worker_heartbeat(worker_id))
        pipe.srem(keys.WORKER_INDEX, str(worker_id))
        await pipe.execute()

    async def list_alive(self) -> list[WorkerHeartbeat]:
        worker_ids = sorted(await self._redis.smembers(keys.WORKER_INDEX))
        if not worker_ids:
            return []

        raw_values = await self._redis.mget(
            [keys.worker_heartbeat(worker_id) for worker_id in worker_ids]
        )

        alive: list[WorkerHeartbeat] = []
        expired: list[str] = []
        for worker_id, raw in zip(worker_ids, raw_values, strict=True):
            if raw is None:
                expired.append(worker_id)
                continue
            try:
                alive.append(WorkerHeartbeat.from_json(raw))
            except (ValueError, TypeError):
                logger.warning("worker_heartbeat_unparsable", worker_id=worker_id)
                expired.append(worker_id)

        if expired:
            await self._redis.srem(keys.WORKER_INDEX, *expired)

        return alive

    async def count_healthy(self) -> int:
        return sum(1 for hb in await self.list_alive() if hb.status == STATUS_HEALTHY)


def build_heartbeat(
    *,
    worker_id: str,
    name: str,
    hostname: str,
    pid: int,
    version: str,
    started_at: datetime,
    status: str = STATUS_STARTING,
) -> WorkerHeartbeat:
    now = utcnow()
    return WorkerHeartbeat(
        worker_id=worker_id,
        name=name,
        status=status,
        hostname=hostname,
        pid=pid,
        version=version,
        started_at=started_at.isoformat(),
        updated_at=now.isoformat(),
    )
