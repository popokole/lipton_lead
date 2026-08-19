"""JWT-токены доступа и обновления (ТЗ §35).

Два токена с разным сроком жизни: короткий access для запросов и длинный
refresh для продления сессии. `type` внутри токена обязателен — иначе refresh
можно было бы предъявить как access и обойти короткий срок жизни.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

import jwt

from app.core.clock import utcnow
from app.core.config import Settings
from app.core.errors import AuthenticationError
from app.models import UserRole

TokenType = Literal["access", "refresh"]


@dataclass(frozen=True, slots=True)
class TokenPayload:
    user_id: uuid.UUID
    role: UserRole
    type: TokenType


def create_access_token(settings: Settings, user_id: uuid.UUID, role: UserRole) -> str:
    return _encode(
        settings,
        user_id,
        role,
        "access",
        timedelta(minutes=settings.access_token_ttl_minutes),
    )


def create_refresh_token(settings: Settings, user_id: uuid.UUID, role: UserRole) -> str:
    return _encode(
        settings, user_id, role, "refresh", timedelta(days=settings.refresh_token_ttl_days)
    )


def decode_token(settings: Settings, token: str, expected: TokenType) -> TokenPayload:
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("Срок действия токена истёк") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("Недействительный токен") from exc

    if claims.get("type") != expected:
        raise AuthenticationError("Токен не того типа")

    try:
        return TokenPayload(
            user_id=uuid.UUID(str(claims["sub"])),
            role=UserRole(str(claims["role"])),
            type=expected,
        )
    except (KeyError, ValueError) as exc:
        raise AuthenticationError("Повреждённый токен") from exc


def _encode(
    settings: Settings,
    user_id: uuid.UUID,
    role: UserRole,
    token_type: TokenType,
    lifetime: timedelta,
) -> str:
    issued_at = utcnow()
    payload = {
        "sub": str(user_id),
        "role": role.value,
        "type": token_type,
        "iat": issued_at,
        "exp": issued_at + lifetime,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(
        payload, settings.jwt_secret.get_secret_value(), algorithm=settings.jwt_algorithm
    )
