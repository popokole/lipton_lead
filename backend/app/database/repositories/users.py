"""Пользователи панели."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utcnow
from app.models import User, UserRole


class UserRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get(self, user_id: uuid.UUID) -> User | None:
        return await self._db.get(User, user_id)

    async def by_email(self, email: str) -> User | None:
        # Адрес приводим к нижнему регистру: «Admin@» и «admin@» — один человек.
        return await self._db.scalar(select(User).where(User.email == email.strip().lower()))

    async def list_all(self) -> list[User]:
        rows = await self._db.scalars(select(User).order_by(User.created_at))
        return list(rows.all())

    async def count(self) -> int:
        return int(await self._db.scalar(select(func.count()).select_from(User)) or 0)

    async def create(
        self, *, email: str, password_hash: str, role: UserRole, full_name: str | None = None
    ) -> User:
        user = User(
            email=email.strip().lower(),
            password_hash=password_hash,
            role=role,
            full_name=full_name,
        )
        self._db.add(user)
        await self._db.flush()
        return user

    async def mark_login(self, user_id: uuid.UUID) -> None:
        user = await self._db.get(User, user_id)
        if user is not None:
            user.last_login_at = utcnow()

    async def update_password(self, user_id: uuid.UUID, password_hash: str) -> None:
        user = await self._db.get(User, user_id)
        if user is not None:
            user.password_hash = password_hash
