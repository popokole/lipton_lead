"""Воркеры и сводка для дашборда (ТЗ §26, §29)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import Select, func, select
from sqlalchemy.orm import InstrumentedAttribute

from app.api.deps import CurrentUser, DbDep, RuntimeDep
from app.core.clock import utcnow
from app.models import (
    Account,
    AccountStatus,
    Action,
    ActionStatus,
    AIRequest,
    Chat,
    EventLog,
    Lead,
    Message,
)
from app.schemas.resources import DailyPoint, DashboardCounters, DashboardSeries, WorkerOut

workers_router = APIRouter(prefix="/workers", tags=["workers"])
analytics_router = APIRouter(prefix="/analytics", tags=["analytics"])


@workers_router.get("", response_model=list[WorkerOut], summary="Живые воркеры")
async def list_workers(_user: CurrentUser, runtime: RuntimeDep) -> list[WorkerOut]:
    """Состав берётся из Redis: там пульс, а не из таблицы с историей."""
    heartbeats = await runtime.worker_registry.list_alive()
    return [
        WorkerOut(
            id=hb.worker_id,
            name=hb.name,
            status=hb.status,
            hostname=hb.hostname,
            pid=hb.pid,
            version=hb.version,
            accounts=hb.accounts,
            accounts_count=hb.accounts_count,
            queue_size=hb.queue_size,
            last_error=hb.last_error,
            updated_at=hb.updated_at,
            alive=True,
        )
        for hb in heartbeats
    ]


@analytics_router.get("/dashboard", response_model=DashboardCounters, summary="Счётчики дашборда")
async def dashboard(_user: CurrentUser, db: DbDep, runtime: RuntimeDep) -> DashboardCounters:
    since = utcnow() - timedelta(days=1)

    async def count(stmt: Select[Any]) -> int:
        return int(await db.scalar(stmt) or 0)

    accounts_total = await count(select(func.count()).select_from(Account))
    accounts_online = await count(
        select(func.count()).select_from(Account).where(Account.status == AccountStatus.ONLINE)
    )
    chats_monitored = await count(
        select(func.count()).select_from(Chat).where(Chat.monitored.is_(True))
    )
    messages_today = await count(
        select(func.count()).select_from(Message).where(Message.created_at >= since)
    )
    ai_today = await count(
        select(func.count()).select_from(AIRequest).where(AIRequest.created_at >= since)
    )
    replies_today = await count(
        select(func.count())
        .select_from(Action)
        .where(Action.sent_at >= since, Action.status == ActionStatus.SENT)
    )
    leads_total = await count(select(func.count()).select_from(Lead))
    errors_today = await count(
        select(func.count())
        .select_from(EventLog)
        .where(EventLog.ts >= since, EventLog.level == "WARNING")
    )
    workers_healthy = await runtime.worker_registry.count_healthy()

    return DashboardCounters(
        accounts_total=accounts_total,
        accounts_online=accounts_online,
        chats_monitored=chats_monitored,
        messages_today=messages_today,
        ai_analyzed_today=ai_today,
        replies_today=replies_today,
        leads_total=leads_total,
        errors_today=errors_today,
        workers_healthy=workers_healthy,
    )


@analytics_router.get("/series", response_model=DashboardSeries, summary="Графики по дням")
async def series(
    _user: CurrentUser, db: DbDep, days: int = Query(default=14, ge=1, le=90)
) -> DashboardSeries:
    since = utcnow() - timedelta(days=days)

    async def by_day(
        column: InstrumentedAttribute[Any], source: Any, *conditions: Any
    ) -> list[DailyPoint]:
        day = func.date_trunc("day", column).label("day")
        stmt = (
            select(day, func.count())
            .select_from(source)
            .where(column >= since, *conditions)
            .group_by(day)
            .order_by(day)
        )
        rows = await db.execute(stmt)
        return [DailyPoint(day=row[0].date().isoformat(), value=int(row[1])) for row in rows]

    return DashboardSeries(
        messages=await by_day(Message.created_at, Message),
        matches=await by_day(Message.created_at, Message, Message.rule_id.is_not(None)),
        replies=await by_day(Action.sent_at, Action, Action.status == ActionStatus.SENT),
        leads=await by_day(Lead.first_seen_at, Lead),
        errors=await by_day(EventLog.ts, EventLog, EventLog.level == "WARNING"),
    )
