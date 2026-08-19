"""Управление подключением к PostgreSQL.

Движок создаётся один раз на процесс в lifespan и закрывается при остановке.
Глобального неявного соединения нет: и API, и воркер работают через один и тот
же объект Database, что делает shutdown детерминированным.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings
from app.core.errors import DatabaseError
from app.core.logging import get_logger

logger = get_logger(__name__)


class Database:
    """Владелец пула соединений."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._engine: AsyncEngine | None = None
        self._sessionmaker: async_sessionmaker[AsyncSession] | None = None

    async def connect(self) -> None:
        if self._engine is not None:
            return
        self._engine = create_async_engine(
            self._settings.database_url,
            echo=self._settings.db_echo,
            pool_size=self._settings.db_pool_size,
            max_overflow=self._settings.db_max_overflow,
            pool_timeout=self._settings.db_pool_timeout,
            pool_pre_ping=True,
            pool_recycle=1800,
            # Без кэша prepared statements: после миграции (новые колонки)
            # закэшированные asyncpg запросы бьют InvalidCachedStatementError,
            # пока пул не переработается. Ценой микроскопической скорости
            # избавляемся от этого класса ошибок насовсем.
            connect_args={"statement_cache_size": 0},
        )
        self._sessionmaker = async_sessionmaker(
            bind=self._engine, expire_on_commit=False, autoflush=False
        )
        logger.info("database_connected", pool_size=self._settings.db_pool_size)

    async def disconnect(self) -> None:
        if self._engine is None:
            return
        await self._engine.dispose()
        self._engine = None
        self._sessionmaker = None
        logger.info("database_disconnected")

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise DatabaseError("Database engine is not initialised")
        return self._engine

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Unit of work: коммит на успешном выходе, откат на исключении."""
        if self._sessionmaker is None:
            raise DatabaseError("Database engine is not initialised")
        session = self._sessionmaker()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def ping(self) -> bool:
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        # Health-проверка не должна падать ни от одной ошибки драйвера.
        except Exception as exc:  # noqa: BLE001
            logger.warning("database_ping_failed", detail=str(exc))
            return False
        return True
