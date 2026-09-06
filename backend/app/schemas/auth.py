"""Схемы авторизации в панели."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models import UserRole
from app.schemas.common import ORMModel


class LoginRequest(BaseModel):
    # Не EmailStr: bootstrap.py заводит admin_email из настроек без всякой
    # проверки формата (см. ADMIN_EMAIL=admin@lipton.local в .env) — панель
    # внутренняя, реальная доставляемость почты тут не при делах. EmailStr
    # (email-validator) отдельно отклоняет .local/.test/.invalid как
    # «special-use domain», из-за чего штатный admin-аккаунт не мог залогиниться.
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=256)


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class UserOut(ORMModel):
    """Пароль сюда не попадает даже случайно: поля просто нет."""

    id: uuid.UUID
    email: str
    full_name: str | None
    role: UserRole
    is_active: bool
    last_login_at: datetime | None


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class CreateUserRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=256)
    full_name: str | None = Field(default=None, max_length=200)
    role: UserRole = UserRole.VIEWER
