"""Задержки повторного ответа (ТЗ §20).

Это защита от повторных действий внутри приложения: чтобы система не написала
одному человеку пять раз за минуту и не завалила чат ответами. Средством
обхода ограничений Telegram cooldown не является и не может быть — он только
уменьшает нашу собственную активность.

Проверка выполняется дважды и по разным причинам:

* `check()` — до обращения к AI. Дешёвое чтение, которое экономит деньги:
  генерировать ответ, который всё равно не будет отправлен, бессмысленно.
* `claim()` — прямо перед отправкой, атомарно. Между проверкой и отправкой
  проходит время генерации (секунды), и за это время может прийти второе
  сообщение того же человека. Без атомарного захвата оба дошли бы до отправки.

Захват выполняется одним скриптом Lua: либо заняты все области сразу, либо ни
одна. Иначе при частичном захвате остаются висеть ключи, блокирующие ответы,
которых не было.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from redis.asyncio import Redis

from app.bus import keys
from app.core.logging import get_logger
from app.rules.engine import CooldownSpec

logger = get_logger(__name__)

# Занимает все ключи разом, если ни один не занят. Возвращает 1 при успехе и
# имя занятого ключа при отказе — оператору важно знать, какая именно задержка
# помешала ответу.
_CLAIM = """
for i, key in ipairs(KEYS) do
    if redis.call('exists', key) == 1 then
        return key
    end
end
for i, key in ipairs(KEYS) do
    redis.call('set', key, ARGV[1], 'PX', tonumber(ARGV[i + 1]))
end
return 1
"""

_RELEASE = """
for i, key in ipairs(KEYS) do
    if redis.call('get', key) == ARGV[1] then
        redis.call('del', key)
    end
end
return 1
"""


@dataclass(frozen=True, slots=True)
class CooldownKeys:
    """Кого именно ограничиваем."""

    account_id: uuid.UUID
    tg_chat_id: int | None = None
    peer_tg_id: int | None = None
    rule_id: uuid.UUID | None = None
    scenario_id: uuid.UUID | None = None

    def scopes(self, spec: CooldownSpec) -> list[tuple[str, str, int]]:
        """Пары «ключ Redis — задержка» для всех заданных областей."""
        result: list[tuple[str, str, int]] = []
        if spec.user > 0 and self.peer_tg_id is not None:
            result.append(
                ("user", keys.cooldown("user", self.account_id, self.peer_tg_id), spec.user)
            )
        if spec.chat > 0 and self.tg_chat_id is not None:
            result.append(
                ("chat", keys.cooldown("chat", self.account_id, self.tg_chat_id), spec.chat)
            )
        if spec.account > 0:
            result.append(("account", keys.cooldown("account", self.account_id), spec.account))
        if spec.rule > 0 and self.rule_id is not None:
            result.append(("rule", keys.cooldown("rule", self.account_id, self.rule_id), spec.rule))
        if spec.scenario > 0 and self.scenario_id is not None:
            result.append(
                (
                    "scenario",
                    keys.cooldown("scenario", self.account_id, self.scenario_id),
                    spec.scenario,
                )
            )
        return result


@dataclass(frozen=True, slots=True)
class CooldownVerdict:
    allowed: bool
    blocked_by: str | None = None
    retry_after_seconds: int | None = None

    def __bool__(self) -> bool:
        return self.allowed


@dataclass(frozen=True, slots=True)
class CooldownClaim:
    """Занятые области. Нужен, чтобы освободить их при неудачной отправке."""

    token: str
    redis_keys: tuple[str, ...] = field(default=())


class CooldownGuard:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._claim = redis.register_script(_CLAIM)
        self._release = redis.register_script(_RELEASE)

    async def check(self, cooldown_keys: CooldownKeys, spec: CooldownSpec) -> CooldownVerdict:
        """Дешёвая проверка перед обращением к AI."""
        scopes = cooldown_keys.scopes(spec)
        if not scopes:
            return CooldownVerdict(allowed=True)

        ttls = await self._redis.mget([key for _scope, key, _seconds in scopes])
        for (scope, key, _seconds), value in zip(scopes, ttls, strict=True):
            if value is not None:
                remaining = await self._redis.ttl(key)
                return CooldownVerdict(
                    allowed=False,
                    blocked_by=scope,
                    retry_after_seconds=remaining if remaining and remaining > 0 else None,
                )
        return CooldownVerdict(allowed=True)

    async def claim(
        self, cooldown_keys: CooldownKeys, spec: CooldownSpec
    ) -> tuple[CooldownVerdict, CooldownClaim | None]:
        """Атомарно занимает все области перед отправкой."""
        scopes = cooldown_keys.scopes(spec)
        if not scopes:
            return CooldownVerdict(allowed=True), CooldownClaim(token="", redis_keys=())

        token = uuid.uuid4().hex
        redis_keys = [key for _scope, key, _seconds in scopes]
        args = [token, *[str(seconds * 1000) for _scope, _key, seconds in scopes]]

        result = await self._claim(keys=redis_keys, args=args)
        if str(result) != "1":
            blocked_key = str(result)
            scope = next(
                (scope for scope, key, _seconds in scopes if key == blocked_key), "unknown"
            )
            remaining = await self._redis.ttl(blocked_key)
            logger.debug("cooldown_blocked", scope=scope, account_id=str(cooldown_keys.account_id))
            return (
                CooldownVerdict(
                    allowed=False,
                    blocked_by=scope,
                    retry_after_seconds=remaining if remaining and remaining > 0 else None,
                ),
                None,
            )

        return CooldownVerdict(allowed=True), CooldownClaim(
            token=token, redis_keys=tuple(redis_keys)
        )

    async def release(self, claim: CooldownClaim | None) -> None:
        """Возвращает области, если отправка не состоялась.

        Без этого неудачная отправка молча запрещала бы следующую попытку на
        всё время задержки.
        """
        if claim is None or not claim.redis_keys:
            return
        await self._release(keys=list(claim.redis_keys), args=[claim.token])
