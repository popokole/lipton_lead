"""Глобальная личность ИИ-собеседника.

Одна строка на всю систему: характер и примеры переписки. Подмешивается в
каждую генерацию поверх промпта сценария (см. app/ai/generator.py).
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import AdminUser, CurrentUser, DbDep
from app.models.persona import SINGLETON_ID, Persona

router = APIRouter(prefix="/persona", tags=["persona"])


class PersonaOut(BaseModel):
    enabled: bool
    character: str | None
    examples: str | None
    # Глобальные настройки «в целом» — действуют всегда, вне зависимости от enabled.
    base_rules: str | None
    max_reply_length: int | None


class PersonaUpdate(BaseModel):
    enabled: bool | None = None
    character: str | None = Field(default=None, max_length=20000)
    examples: str | None = Field(default=None, max_length=20000)
    base_rules: str | None = Field(default=None, max_length=20000)
    max_reply_length: int | None = Field(default=None, ge=0, le=100000)


def _to_out(row: Persona) -> PersonaOut:
    return PersonaOut(
        enabled=row.enabled,
        character=row.character,
        examples=row.examples,
        base_rules=row.base_rules,
        max_reply_length=row.max_reply_length,
    )


async def _get_or_create(db: DbDep) -> Persona:
    row = await db.get(Persona, SINGLETON_ID)
    if row is None:
        row = Persona(id=SINGLETON_ID)
        db.add(row)
        await db.flush()
    return row


@router.get("", response_model=PersonaOut, summary="Личность собеседника")
async def get_persona(_user: CurrentUser, db: DbDep) -> PersonaOut:
    row = await _get_or_create(db)
    return _to_out(row)


@router.put("", response_model=PersonaOut, summary="Сохранить личность")
async def update_persona(payload: PersonaUpdate, _admin: AdminUser, db: DbDep) -> PersonaOut:
    row = await _get_or_create(db)
    if payload.enabled is not None:
        row.enabled = payload.enabled
    if payload.character is not None:
        row.character = payload.character.strip() or None
    if payload.examples is not None:
        row.examples = payload.examples.strip() or None
    if payload.base_rules is not None:
        row.base_rules = payload.base_rules.strip() or None
    if payload.max_reply_length is not None:
        # 0 — «без общего ограничения» (то же, что пусто).
        row.max_reply_length = payload.max_reply_length or None
    await db.flush()
    return _to_out(row)
