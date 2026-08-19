"""Аренда аккаунта воркером.

Один Telegram-аккаунт обслуживает ровно один процесс. Если два воркера
подключатся к одной сессии, Telegram ответит AuthKeyDuplicatedError и разлогинит
аккаунт — то есть цена ошибки не «дубль сообщения», а потеря авторизации.

Аренда — ключ в Redis с TTL, который держатель продлевает чаще, чем тот истекает.
Продление и освобождение выполняются скриптами Lua: проверка «мой ли ключ» и
действие должны быть одной атомарной операцией, иначе воркер, чья аренда уже
истекла и перешла к другому, может продлить или снять чужую.
"""

from __future__ import annotations

from uuid import UUID

from redis.asyncio import Redis

from app.bus import keys
from app.core.logging import get_logger

logger = get_logger(__name__)

# Продлить, только если ключ всё ещё наш.
_RENEW = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""

# Снять, только если ключ всё ещё наш.
_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


class AccountLease:
    def __init__(self, redis: Redis, worker_id: str, ttl_seconds: int) -> None:
        self._redis = redis
        self._worker_id = worker_id
        self._ttl_ms = ttl_seconds * 1000
        self._renew = redis.register_script(_RENEW)
        self._release = redis.register_script(_RELEASE)

    @property
    def worker_id(self) -> str:
        return self._worker_id

    async def try_acquire(self, account_id: UUID | str) -> bool:
        acquired = await self._redis.set(
            keys.account_lease(account_id), self._worker_id, nx=True, px=self._ttl_ms
        )
        if acquired:
            logger.info("account_lease_acquired", account_id=str(account_id))
        return bool(acquired)

    async def renew(self, account_id: UUID | str) -> bool:
        result = await self._renew(
            keys=[keys.account_lease(account_id)], args=[self._worker_id, self._ttl_ms]
        )
        return bool(result)

    async def release(self, account_id: UUID | str) -> None:
        await self._release(keys=[keys.account_lease(account_id)], args=[self._worker_id])
        logger.info("account_lease_released", account_id=str(account_id))

    async def holder(self, account_id: UUID | str) -> str | None:
        """Кто сейчас держит аренду. Нужно API, чтобы адресовать команду."""
        value = await self._redis.get(keys.account_lease(account_id))
        return str(value) if value is not None else None
