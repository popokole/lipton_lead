"""Фикстуры для тестов, которым нужна живая база.

Каждый тест работает во внешней транзакции, которая откатывается на выходе:
проверки не оставляют за собой строк и не зависят от порядка запуска.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.bus.connection import RedisProvider
from app.core.config import Settings
from app.database.session import Database
from app.models import Account, AccountStatus, Chat, ChatType


@pytest.fixture
async def database(integration_settings: Settings) -> AsyncIterator[Database]:
    db = Database(integration_settings)
    await db.connect()
    yield db
    await db.disconnect()


@pytest.fixture
async def redis_client(integration_settings: Settings) -> AsyncIterator[Redis]:
    """Чистая база Redis на тест: ключи не должны протекать между проверками."""
    provider = RedisProvider(integration_settings)
    await provider.connect()
    client = provider.client
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await provider.disconnect()


@pytest.fixture
async def db(database: Database) -> AsyncIterator[AsyncSession]:
    async with database.engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()


@pytest.fixture
async def account(db: AsyncSession) -> Account:
    item = Account(
        label="test-account",
        tg_user_id=_unique_tg_id(),
        username="tester",
        display_name="Tester",
        status=AccountStatus.ONLINE,
    )
    db.add(item)
    await db.flush()
    return item


@pytest.fixture
async def chat(db: AsyncSession, account: Account) -> Chat:
    item = Chat(
        account_id=account.id,
        tg_chat_id=_unique_tg_id(),
        type=ChatType.SUPERGROUP,
        title="Test chat",
        monitored=True,
    )
    db.add(item)
    await db.flush()
    return item


def _unique_tg_id() -> int:
    """Идентификатор, не пересекающийся с другими тестами в той же базе."""
    return uuid.uuid4().int % 10_000_000_000
