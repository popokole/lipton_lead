"""Контейнер долгоживущих ресурсов процесса.

Один и тот же Runtime используют и API, и воркер: у обоих есть PostgreSQL,
Redis и реестр воркеров, различается только то, что они поверх этого делают.
Ресурсы создаются в startup и освобождаются в shutdown — глобальных
самоинициализирующихся синглтонов нет, иначе тесты и graceful shutdown ломаются.
"""

from __future__ import annotations

from app.bus.connection import RedisProvider
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.database.session import Database
from app.workers.registry import WorkerRegistry

logger = get_logger(__name__)

VERSION = "0.1.0"


class Runtime:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.database = Database(self.settings)
        self.redis = RedisProvider(self.settings)
        self._worker_registry: WorkerRegistry | None = None

    @property
    def worker_registry(self) -> WorkerRegistry:
        if self._worker_registry is None:
            self._worker_registry = WorkerRegistry(
                self.redis.client, ttl_seconds=self.settings.worker_stale_after_seconds
            )
        return self._worker_registry

    async def startup(self) -> None:
        await self.database.connect()
        await self.redis.connect()
        logger.info(
            "runtime_started",
            env=self.settings.env,
            version=VERSION,
            app=self.settings.app_name,
        )

    async def shutdown(self) -> None:
        self._worker_registry = None
        await self.redis.disconnect()
        await self.database.disconnect()
        logger.info("runtime_stopped")
