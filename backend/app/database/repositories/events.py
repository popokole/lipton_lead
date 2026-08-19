"""Журнал событий обработки (ТЗ §30).

Пишется в базу, потому что панель обязана показывать путь конкретного
сообщения: получено → правило → AI → действие. Секретов здесь не бывает —
в `extra` попадают только идентификаторы, длительности и причины отказа.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utcnow
from app.database.base import affected_rows
from app.models import EventLog, EventType

# Длина колонки event_logs.status.
STATUS_MAX_LENGTH = 32


class EventLogRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def add(
        self,
        event_type: EventType,
        *,
        level: str = "INFO",
        account_id: uuid.UUID | None = None,
        chat_id: uuid.UUID | None = None,
        message_id: uuid.UUID | None = None,
        scenario_id: uuid.UUID | None = None,
        rule_id: uuid.UUID | None = None,
        action_id: uuid.UUID | None = None,
        worker_id: uuid.UUID | None = None,
        duration_ms: int | None = None,
        status: str | None = None,
        error: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        # status — короткий машинный код (колонка на 32 символа), а не место
        # для фразы. Длинную причину не роняем вставкой, а переносим в error,
        # где нет ограничения по длине: журнал важнее аккуратности типа.
        if status is not None and len(status) > STATUS_MAX_LENGTH:
            error = error or status
            status = status[:STATUS_MAX_LENGTH]

        self._db.add(
            EventLog(
                ts=utcnow(),
                level=level,
                event_type=event_type,
                account_id=account_id,
                chat_id=chat_id,
                message_id=message_id,
                scenario_id=scenario_id,
                rule_id=rule_id,
                action_id=action_id,
                worker_id=worker_id,
                duration_ms=duration_ms,
                status=status,
                error=error,
                extra=extra,
            )
        )

    async def recent(
        self,
        limit: int = 100,
        *,
        event_type: EventType | None = None,
        account_id: uuid.UUID | None = None,
    ) -> list[EventLog]:
        stmt = select(EventLog).order_by(desc(EventLog.ts)).limit(limit)
        if event_type is not None:
            stmt = stmt.where(EventLog.event_type == event_type)
        if account_id is not None:
            stmt = stmt.where(EventLog.account_id == account_id)
        return list((await self._db.scalars(stmt)).all())

    async def purge_before(self, cutoff: datetime) -> int:
        result = await self._db.execute(delete(EventLog).where(EventLog.ts < cutoff))
        return affected_rows(result)

    async def purge_older_than_days(self, days: int) -> int:
        return await self.purge_before(utcnow() - timedelta(days=days))
