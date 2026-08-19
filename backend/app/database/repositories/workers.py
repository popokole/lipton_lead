"""Долговременная карточка воркера в PostgreSQL.

Живой heartbeat лежит в Redis; сюда пишется редко — при старте, изменении
статуса и остановке. Регулярный пульс каждого воркера не должен превращаться
в постоянный поток UPDATE по базе.
"""

from __future__ import annotations

import uuid

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utcnow
from app.models import Worker, WorkerStatus


class WorkerRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def register(
        self,
        worker_id: uuid.UUID,
        *,
        name: str,
        hostname: str,
        pid: int,
        version: str,
    ) -> Worker:
        worker = Worker(
            id=worker_id,
            name=name,
            hostname=hostname,
            pid=pid,
            version=version,
            status=WorkerStatus.STARTING,
            started_at=utcnow(),
            heartbeat_at=utcnow(),
        )
        self._db.add(worker)
        await self._db.flush()
        return worker

    async def set_status(
        self,
        worker_id: uuid.UUID,
        status: WorkerStatus,
        *,
        accounts_count: int | None = None,
        last_error: str | None = None,
    ) -> None:
        values: dict[str, object] = {"status": status, "heartbeat_at": utcnow()}
        if accounts_count is not None:
            values["accounts_count"] = accounts_count
        if last_error is not None:
            values["last_error"] = last_error
        if status is WorkerStatus.STOPPED:
            values["stopped_at"] = utcnow()

        await self._db.execute(update(Worker).where(Worker.id == worker_id).values(**values))
