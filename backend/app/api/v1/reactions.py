"""Настройки авто-реакций на посты (прогрев). Одна строка на систему."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import AdminUser, CurrentUser, DbDep
from app.models.reaction_settings import SINGLETON_ID, ReactionSettings

router = APIRouter(prefix="/reactions", tags=["reactions"])


class ReactionSettingsOut(BaseModel):
    enabled: bool
    per_hour: int
    fresh_window_hours: int
    per_chat_cooldown_minutes: int


class ReactionSettingsUpdate(BaseModel):
    enabled: bool | None = None
    per_hour: int | None = Field(default=None, ge=1, le=500)
    fresh_window_hours: int | None = Field(default=None, ge=1, le=168)
    per_chat_cooldown_minutes: int | None = Field(default=None, ge=1, le=1440)


async def _get_or_create(db: DbDep) -> ReactionSettings:
    row = await db.get(ReactionSettings, SINGLETON_ID)
    if row is None:
        row = ReactionSettings(id=SINGLETON_ID)
        db.add(row)
        await db.flush()
    return row


def _to_out(row: ReactionSettings) -> ReactionSettingsOut:
    return ReactionSettingsOut(
        enabled=row.enabled,
        per_hour=row.per_hour,
        fresh_window_hours=row.fresh_window_hours,
        per_chat_cooldown_minutes=row.per_chat_cooldown_minutes,
    )


@router.get("", response_model=ReactionSettingsOut, summary="Настройки авто-реакций")
async def get_reaction_settings(_user: CurrentUser, db: DbDep) -> ReactionSettingsOut:
    return _to_out(await _get_or_create(db))


@router.put("", response_model=ReactionSettingsOut, summary="Сохранить настройки реакций")
async def update_reaction_settings(
    payload: ReactionSettingsUpdate, _admin: AdminUser, db: DbDep
) -> ReactionSettingsOut:
    row = await _get_or_create(db)
    if payload.enabled is not None:
        row.enabled = payload.enabled
    if payload.per_hour is not None:
        row.per_hour = payload.per_hour
    if payload.fresh_window_hours is not None:
        row.fresh_window_hours = payload.fresh_window_hours
    if payload.per_chat_cooldown_minutes is not None:
        row.per_chat_cooldown_minutes = payload.per_chat_cooldown_minutes
    await db.flush()
    return _to_out(row)
