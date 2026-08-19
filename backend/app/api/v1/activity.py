"""Сообщения, действия, журнал, диалоги и лиды (ТЗ §26)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import Select, func, select

from app.api.deps import CurrentUser, DbDep
from app.models import Action, ActionStatus, Chat, Conversation, EventLog, EventType, Lead, Message
from app.schemas.common import Page
from app.schemas.resources import (
    ActionOut,
    ConversationOut,
    EventLogOut,
    LeadOut,
    MessageOut,
)

messages_router = APIRouter(prefix="/messages", tags=["messages"])
actions_router = APIRouter(prefix="/actions", tags=["actions"])
logs_router = APIRouter(prefix="/logs", tags=["logs"])
conversations_router = APIRouter(prefix="/conversations", tags=["conversations"])
leads_router = APIRouter(prefix="/leads", tags=["leads"])


async def _count(db: DbDep, stmt: Select[Any]) -> int:
    """Общее число строк для постраничного вывода."""
    subquery = stmt.order_by(None).subquery()
    return int(await db.scalar(select(func.count()).select_from(subquery)) or 0)


@messages_router.get("", response_model=Page[MessageOut], summary="Сообщения")
async def list_messages(
    _user: CurrentUser,
    db: DbDep,
    account_id: uuid.UUID | None = Query(default=None),
    chat_id: uuid.UUID | None = Query(default=None),
    matched_only: bool = Query(default=False),
    search: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[MessageOut]:
    # Канал (название и наличие аватара) берём джойном к чату — иначе на
    # каждое сообщение был бы отдельный запрос за его чатом.
    stmt = (
        select(
            Message,
            Chat.title.label("chat_title"),
            Chat.username.label("chat_username"),
            (Chat.avatar.is_not(None)).label("chat_has_avatar"),
        )
        .outerjoin(Chat, Chat.id == Message.chat_id)
        .order_by(Message.date.desc())
    )
    if account_id is not None:
        stmt = stmt.where(Message.account_id == account_id)
    if chat_id is not None:
        stmt = stmt.where(Message.chat_id == chat_id)
    if matched_only:
        stmt = stmt.where(Message.rule_id.is_not(None))
    if search:
        stmt = stmt.where(Message.text.ilike(f"%{search}%"))

    count_stmt = select(Message.id).select_from(Message).order_by(None)
    if account_id is not None:
        count_stmt = count_stmt.where(Message.account_id == account_id)
    if chat_id is not None:
        count_stmt = count_stmt.where(Message.chat_id == chat_id)
    if matched_only:
        count_stmt = count_stmt.where(Message.rule_id.is_not(None))
    if search:
        count_stmt = count_stmt.where(Message.text.ilike(f"%{search}%"))
    total = await _count(db, count_stmt)

    items: list[MessageOut] = []
    for row in (await db.execute(stmt.limit(limit).offset(offset))).all():
        message = MessageOut.model_validate(row[0])
        items.append(
            message.model_copy(
                update={
                    "chat_title": row.chat_title,
                    "chat_username": row.chat_username,
                    "chat_has_avatar": bool(row.chat_has_avatar),
                }
            )
        )
    return Page(items=items, total=total, limit=limit, offset=offset)


@actions_router.get("", response_model=Page[ActionOut], summary="Действия")
async def list_actions(
    _user: CurrentUser,
    db: DbDep,
    account_id: uuid.UUID | None = Query(default=None),
    status: ActionStatus | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[ActionOut]:
    stmt = select(Action).order_by(Action.created_at.desc())
    if account_id is not None:
        stmt = stmt.where(Action.account_id == account_id)
    if status is not None:
        stmt = stmt.where(Action.status == status)

    total = await _count(db, stmt)
    rows = await db.scalars(stmt.limit(limit).offset(offset))
    return Page(
        items=[ActionOut.model_validate(row) for row in rows.all()],
        total=total,
        limit=limit,
        offset=offset,
    )


@logs_router.get("", response_model=Page[EventLogOut], summary="Журнал обработки")
async def list_logs(
    _user: CurrentUser,
    db: DbDep,
    account_id: uuid.UUID | None = Query(default=None),
    event_type: EventType | None = Query(default=None),
    level: str | None = Query(default=None, max_length=16),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> Page[EventLogOut]:
    base = select(EventLog).order_by(EventLog.ts.desc())
    if account_id is not None:
        base = base.where(EventLog.account_id == account_id)
    if event_type is not None:
        base = base.where(EventLog.event_type == event_type)
    if level:
        base = base.where(EventLog.level == level.upper())

    total = await _count(
        db,
        select(EventLog.id).order_by(None).where(base.whereclause)
        if base.whereclause is not None
        else select(EventLog.id),
    )

    enriched = (
        select(
            EventLog,
            Chat.title.label("chat_title"),
            Chat.username.label("chat_username"),
            (Chat.avatar.is_not(None)).label("chat_has_avatar"),
        )
        .select_from(EventLog)
        .outerjoin(Chat, Chat.id == EventLog.chat_id)
        .order_by(EventLog.ts.desc())
    )
    if base.whereclause is not None:
        enriched = enriched.where(base.whereclause)

    items: list[EventLogOut] = []
    for row in (await db.execute(enriched.limit(limit).offset(offset))).all():
        item = EventLogOut.model_validate(row[0])
        items.append(
            item.model_copy(
                update={
                    "chat_title": row.chat_title,
                    "chat_username": row.chat_username,
                    "chat_has_avatar": bool(row.chat_has_avatar),
                }
            )
        )
    return Page(items=items, total=total, limit=limit, offset=offset)


@conversations_router.get("", response_model=list[ConversationOut], summary="Диалоги")
async def list_conversations(
    _user: CurrentUser,
    db: DbDep,
    account_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[ConversationOut]:
    stmt = select(Conversation).order_by(Conversation.last_message_at.desc().nullslast())
    if account_id is not None:
        stmt = stmt.where(Conversation.account_id == account_id)
    rows = await db.scalars(stmt.limit(limit))
    return [ConversationOut.model_validate(row) for row in rows.all()]


@leads_router.get("", response_model=list[LeadOut], summary="Лиды")
async def list_leads(
    _user: CurrentUser,
    db: DbDep,
    account_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[LeadOut]:
    stmt = select(Lead).order_by(Lead.score.desc(), Lead.last_seen_at.desc().nullslast())
    if account_id is not None:
        stmt = stmt.where(Lead.account_id == account_id)
    rows = await db.scalars(stmt.limit(limit))
    return [LeadOut.model_validate(row) for row in rows.all()]
