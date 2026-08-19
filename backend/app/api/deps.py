"""Общие зависимости FastAPI."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.bus.commands import CommandBus
from app.core.config import Settings
from app.core.errors import AuthenticationError
from app.core.runtime import Runtime
from app.database.repositories.users import UserRepository
from app.models import User, UserRole
from app.security.rate_limit import RateLimiter
from app.security.rbac import require_role
from app.security.tokens import decode_token
from app.workers.lease import AccountLease

# auto_error=False: без заголовка хотим свою ошибку в общем формате, а не
# стандартную 403 от Starlette.
bearer_scheme = HTTPBearer(auto_error=False)


def get_runtime(request: Request) -> Runtime:
    return request.app.state.runtime


def get_settings_dep(runtime: Annotated[Runtime, Depends(get_runtime)]) -> Settings:
    return runtime.settings


async def get_db(runtime: Annotated[Runtime, Depends(get_runtime)]) -> AsyncIterator[AsyncSession]:
    async with runtime.database.session() as session:
        yield session


def get_redis(runtime: Annotated[Runtime, Depends(get_runtime)]) -> Redis:
    return runtime.redis.client


def get_rate_limiter(runtime: Annotated[Runtime, Depends(get_runtime)]) -> RateLimiter:
    return RateLimiter(runtime.redis.client)


def get_command_bus(runtime: Annotated[Runtime, Depends(get_runtime)]) -> CommandBus:
    """Шина к воркеру.

    API не держит Telegram-клиентов: всё, что требует живого подключения,
    выполняет владелец аренды аккаунта.
    """
    redis = runtime.redis.client
    # Роль читателя аренды: сам API аккаунты не арендует.
    lease = AccountLease(redis, "api", ttl_seconds=runtime.settings.account_lease_ttl_seconds)
    return CommandBus(redis, lease)


async def get_current_user(
    runtime: Annotated[Runtime, Depends(get_runtime)],
    db: Annotated[AsyncSession, Depends(get_db)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> User:
    if credentials is None:
        raise AuthenticationError("Нужен токен доступа")

    payload = decode_token(runtime.settings, credentials.credentials, "access")
    user = await UserRepository(db).get(payload.user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("Пользователь недоступен")
    return user


async def require_operator(user: Annotated[User, Depends(get_current_user)]) -> User:
    require_role(user.role, UserRole.OPERATOR)
    return user


async def require_admin(user: Annotated[User, Depends(get_current_user)]) -> User:
    require_role(user.role, UserRole.ADMIN)
    return user


RuntimeDep = Annotated[Runtime, Depends(get_runtime)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
DbDep = Annotated[AsyncSession, Depends(get_db)]
RedisDep = Annotated[Redis, Depends(get_redis)]
RateLimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]
CommandBusDep = Annotated[CommandBus, Depends(get_command_bus)]
CurrentUser = Annotated[User, Depends(get_current_user)]
OperatorUser = Annotated[User, Depends(require_operator)]
AdminUser = Annotated[User, Depends(require_admin)]
