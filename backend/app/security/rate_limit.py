"""Ограничение частоты запросов (ТЗ §35).

Счётчик в Redis с окном в минуту. Отдельный, более строгий лимит на вход:
перебор пароля должен упираться в лимит гораздо раньше, чем обычная работа с
панелью.

При недоступном Redis запросы пропускаются. Это осознанный выбор: закрывать
панель целиком из-за сбоя вспомогательного хранилища хуже, чем на время
остаться без лимита.
"""

from __future__ import annotations

from redis.asyncio import Redis

from app.bus import keys
from app.core.errors import RateLimitedError
from app.core.logging import get_logger

logger = get_logger(__name__)

WINDOW_SECONDS = 60


class RateLimiter:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def hit(self, scope: str, identity: str, limit: int) -> None:
        key = keys.cooldown("ratelimit", scope, identity)
        try:
            current = await self._redis.incr(key)
            if current == 1:
                await self._redis.expire(key, WINDOW_SECONDS)
        except Exception as exc:  # noqa: BLE001
            logger.warning("rate_limit_unavailable", detail=str(exc))
            return

        if current > limit:
            ttl = await self._redis.ttl(key)
            raise RateLimitedError(
                f"Слишком много запросов, попробуйте через {max(ttl, 1)} с",
                retry_after=max(ttl, 1),
            )
