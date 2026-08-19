"""Вход в панель (ТЗ §26 /api/auth)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.deps import AdminUser, CurrentUser, DbDep, RateLimiterDep, RuntimeDep
from app.core.errors import AuthenticationError, ConflictError, InvalidInputError
from app.database.repositories.users import UserRepository
from app.schemas.auth import (
    ChangePasswordRequest,
    CreateUserRequest,
    LoginRequest,
    RefreshRequest,
    TokenPair,
    UserOut,
)
from app.schemas.common import Ok
from app.security.passwords import (
    hash_password,
    needs_rehash,
    validate_password_strength,
    verify_password,
)
from app.security.tokens import create_access_token, create_refresh_token, decode_token

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenPair, summary="Вход по email и паролю")
async def login(
    payload: LoginRequest,
    request: Request,
    runtime: RuntimeDep,
    db: DbDep,
    limiter: RateLimiterDep,
) -> TokenPair:
    settings = runtime.settings
    client_ip = request.client.host if request.client else "unknown"
    await limiter.hit("login", client_ip, settings.rate_limit_login_per_minute)

    repository = UserRepository(db)
    user = await repository.by_email(payload.email)

    # Одинаковый ответ на «нет такого пользователя» и «неверный пароль»:
    # иначе форма входа превращается в проверялку существующих адресов.
    if (
        user is None
        or not user.is_active
        or not verify_password(payload.password, user.password_hash)
    ):
        raise AuthenticationError("Неверный email или пароль")

    if needs_rehash(user.password_hash):
        await repository.update_password(user.id, hash_password(payload.password))
    await repository.mark_login(user.id)

    return TokenPair(
        access_token=create_access_token(settings, user.id, user.role),
        refresh_token=create_refresh_token(settings, user.id, user.role),
        expires_in=settings.access_token_ttl_minutes * 60,
    )


@router.post("/refresh", response_model=TokenPair, summary="Продлить сессию")
async def refresh(payload: RefreshRequest, runtime: RuntimeDep, db: DbDep) -> TokenPair:
    settings = runtime.settings
    claims = decode_token(settings, payload.refresh_token, "refresh")

    user = await UserRepository(db).get(claims.user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("Пользователь недоступен")

    return TokenPair(
        access_token=create_access_token(settings, user.id, user.role),
        refresh_token=create_refresh_token(settings, user.id, user.role),
        expires_in=settings.access_token_ttl_minutes * 60,
    )


@router.get("/me", response_model=UserOut, summary="Текущий пользователь")
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)


@router.post("/password", response_model=Ok, summary="Сменить свой пароль")
async def change_password(
    payload: ChangePasswordRequest, user: CurrentUser, runtime: RuntimeDep, db: DbDep
) -> Ok:
    if not verify_password(payload.current_password, user.password_hash):
        raise InvalidInputError("Текущий пароль неверен")

    validate_password_strength(payload.new_password, runtime.settings.password_min_length)
    await UserRepository(db).update_password(user.id, hash_password(payload.new_password))
    return Ok(detail="Пароль обновлён")


@router.get("/users", response_model=list[UserOut], summary="Пользователи панели")
async def list_users(_admin: AdminUser, db: DbDep) -> list[UserOut]:
    return [UserOut.model_validate(user) for user in await UserRepository(db).list_all()]


@router.post("/users", response_model=UserOut, status_code=201, summary="Создать пользователя")
async def create_user(
    payload: CreateUserRequest, _admin: AdminUser, runtime: RuntimeDep, db: DbDep
) -> UserOut:
    repository = UserRepository(db)
    if await repository.by_email(payload.email) is not None:
        raise ConflictError("Пользователь с таким email уже есть")

    validate_password_strength(payload.password, runtime.settings.password_min_length)
    user = await repository.create(
        email=payload.email,
        password_hash=hash_password(payload.password),
        role=payload.role,
        full_name=payload.full_name,
    )
    return UserOut.model_validate(user)
