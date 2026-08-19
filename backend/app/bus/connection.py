"""Подключение к Redis.

Redis обслуживает очереди команд, локи, cooldown и realtime-события. Источником
истины он не является — при полной потере Redis система теряет расписание и
кулдауны, но не данные (ТЗ §24).
"""

from __future__ import annotations

from redis.asyncio import ConnectionPool, Redis

from app.core.config import Settings
from app.core.errors import RedisError
from app.core.logging import get_logger

logger = get_logger(__name__)


class RedisProvider:
    """Владелец пула соединений Redis."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: ConnectionPool | None = None
        self._client: Redis | None = None

    async def connect(self) -> None:
        if self._client is not None:
            return
        self._pool = ConnectionPool.from_url(
            self._settings.redis_url,
            max_connections=self._settings.redis_max_connections,
            decode_responses=True,
            health_check_interval=30,
        )
        self._client = Redis(connection_pool=self._pool)
        logger.info("redis_connected")

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._pool is not None:
            # Пул передан клиенту явно, поэтому Redis.aclose() его не трогает.
            await self._pool.disconnect()
            self._pool = None
        logger.info("redis_disconnected")

    @property
    def client(self) -> Redis:
        if self._client is None:
            raise RedisError("Redis client is not initialised")
        return self._client

    async def ping(self) -> bool:
        try:
            return bool(await self.client.ping())
        # Health-проверка не должна падать ни от одной ошибки клиента.
        except Exception as exc:  # noqa: BLE001
            logger.warning("redis_ping_failed", detail=str(exc))
            return False
