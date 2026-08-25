"""Стоп-лист: кому никогда не отвечаем.

Изменения подхватывает воркер в памяти в течение ~интервала обновления реестра,
поэтому отдельного «применить» не нужно.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import delete as sa_delete
from sqlalchemy import desc, select

from app.api.deps import CurrentUser, DbDep, OperatorUser
from app.core.errors import InvalidInputError, NotFoundError
from app.models import StopEntry
from app.schemas.common import Ok

router = APIRouter(prefix="/stoplist", tags=["stoplist"])


class StopOut(BaseModel):
    id: uuid.UUID
    tg_user_id: int | None
    username: str | None
    note: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class StopCreate(BaseModel):
    tg_user_id: int | None = None
    username: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=255)


@router.get("", response_model=list[StopOut], summary="Стоп-лист")
async def list_stoplist(_user: CurrentUser, db: DbDep) -> list[StopOut]:
    rows = await db.scalars(select(StopEntry).order_by(desc(StopEntry.created_at)).limit(500))
    return [StopOut.model_validate(row) for row in rows.all()]


@router.post("", response_model=StopOut, status_code=201, summary="Добавить в стоп-лист")
async def add_stoplist(payload: StopCreate, _user: OperatorUser, db: DbDep) -> StopOut:
    username = (payload.username or "").lstrip("@").strip() or None
    if payload.tg_user_id is None and not username:
        raise InvalidInputError("Укажите tg-id или @username")
    entry = StopEntry(tg_user_id=payload.tg_user_id, username=username, note=payload.note)
    db.add(entry)
    await db.flush()
    return StopOut.model_validate(entry)


@router.delete("/{entry_id}", response_model=Ok, summary="Убрать из стоп-листа")
async def remove_stoplist(entry_id: uuid.UUID, _user: OperatorUser, db: DbDep) -> Ok:
    entry = await db.get(StopEntry, entry_id)
    if entry is None:
        raise NotFoundError("Запись не найдена")
    await db.execute(sa_delete(StopEntry).where(StopEntry.id == entry_id))
    return Ok()
