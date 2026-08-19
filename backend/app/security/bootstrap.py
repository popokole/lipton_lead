"""Первичный администратор.

Создаётся при первом запуске из ADMIN_EMAIL/ADMIN_PASSWORD. Если пользователи
уже есть, ничего не происходит — иначе изменение переменной окружения молча
создавало бы новый администраторский доступ на работающей системе.
"""

from __future__ import annotations

from app.core.config import Settings
from app.core.logging import get_logger
from app.database.repositories.users import UserRepository
from app.database.session import Database
from app.models import UserRole
from app.security.passwords import hash_password

logger = get_logger(__name__)


async def ensure_admin_exists(settings: Settings, database: Database) -> None:
    async with database.session() as db:
        repository = UserRepository(db)
        if await repository.count() > 0:
            return

        password = settings.admin_password.get_secret_value()
        if len(password) < settings.password_min_length:
            logger.error(
                "admin_password_too_short",
                required=settings.password_min_length,
            )
            return

        await repository.create(
            email=settings.admin_email,
            password_hash=hash_password(password),
            role=UserRole.ADMIN,
            full_name="Администратор",
        )

    logger.info("admin_created", email=settings.admin_email)
