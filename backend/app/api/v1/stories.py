"""Настройки авто-просмотра историй (прогрев).

Одна строка на систему. Воркер читает её и периодически просматривает истории
лидов и недавно активных в чатах. Темп регулируется здесь — из панели.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import AdminUser, CurrentUser, DbDep
from app.models.story_settings import SINGLETON_ID, StorySettings

router = APIRouter(prefix="/stories", tags=["stories"])


class StorySettingsOut(BaseModel):
    enabled: bool
    per_hour: int
    active_window_hours: int
    per_user_cooldown_hours: int


class StorySettingsUpdate(BaseModel):
    enabled: bool | None = None
    per_hour: int | None = Field(default=None, ge=1, le=500)
    active_window_hours: int | None = Field(default=None, ge=1, le=720)
    per_user_cooldown_hours: int | None = Field(default=None, ge=1, le=720)


async def _get_or_create(db: DbDep) -> StorySettings:
    row = await db.get(StorySettings, SINGLETON_ID)
    if row is None:
        row = StorySettings(id=SINGLETON_ID)
        db.add(row)
        await db.flush()
    return row


def _to_out(row: StorySettings) -> StorySettingsOut:
    return StorySettingsOut(
        enabled=row.enabled,
        per_hour=row.per_hour,
        active_window_hours=row.active_window_hours,
        per_user_cooldown_hours=row.per_user_cooldown_hours,
    )


@router.get("", response_model=StorySettingsOut, summary="Настройки просмотра историй")
async def get_story_settings(_user: CurrentUser, db: DbDep) -> StorySettingsOut:
    return _to_out(await _get_or_create(db))


@router.put("", response_model=StorySettingsOut, summary="Сохранить настройки историй")
async def update_story_settings(
    payload: StorySettingsUpdate, _admin: AdminUser, db: DbDep
) -> StorySettingsOut:
    row = await _get_or_create(db)
    if payload.enabled is not None:
        row.enabled = payload.enabled
    if payload.per_hour is not None:
        row.per_hour = payload.per_hour
    if payload.active_window_hours is not None:
        row.active_window_hours = payload.active_window_hours
    if payload.per_user_cooldown_hours is not None:
        row.per_user_cooldown_hours = payload.per_user_cooldown_hours
    await db.flush()
    return _to_out(row)
