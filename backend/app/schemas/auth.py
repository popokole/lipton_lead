"""Схемы авторизации в панели."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.models import UserRole
from app.schemas.common import ORMModel


class LoginRequest(BaseModel):
    email: EmailStr
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
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)
    full_name: str | None = Field(default=None, max_length=200)
    role: UserRole = UserRole.VIEWER
