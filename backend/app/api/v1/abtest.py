"""A/B заходов: варианты первого сообщения на сценарий + их статистика."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import delete as sa_delete
from sqlalchemy import select

from app.api.deps import CurrentUser, DbDep, OperatorUser
from app.core.errors import NotFoundError
from app.models import AbVariant, Scenario
from app.schemas.common import Ok

router = APIRouter(prefix="/ab", tags=["abtest"])


class AbVariantOut(BaseModel):
    id: uuid.UUID
    scenario_id: uuid.UUID
    text: str
    enabled: bool
    sent_count: int
    reply_count: int
    reply_rate: float
    created_at: datetime


class AbVariantCreate(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class AbVariantPatch(BaseModel):
    enabled: bool


def _out(v: AbVariant) -> AbVariantOut:
    rate = round(v.reply_count / v.sent_count, 3) if v.sent_count else 0.0
    return AbVariantOut(
        id=v.id,
        scenario_id=v.scenario_id,
        text=v.text,
        enabled=v.enabled,
        sent_count=v.sent_count,
        reply_count=v.reply_count,
        reply_rate=rate,
        created_at=v.created_at,
    )


@router.get(
    "/{scenario_id}", response_model=list[AbVariantOut], summary="Варианты заходов"
)
async def list_variants(
    scenario_id: uuid.UUID, _user: CurrentUser, db: DbDep
) -> list[AbVariantOut]:
    rows = await db.scalars(
        select(AbVariant)
        .where(AbVariant.scenario_id == scenario_id)
        .order_by(AbVariant.created_at.asc())
    )
    return [_out(v) for v in rows.all()]


@router.post(
    "/{scenario_id}", response_model=AbVariantOut, status_code=201, summary="Добавить заход"
)
async def add_variant(
    scenario_id: uuid.UUID, payload: AbVariantCreate, _user: OperatorUser, db: DbDep
) -> AbVariantOut:
    scenario = await db.get(Scenario, scenario_id)
    if scenario is None:
        raise NotFoundError("Сценарий не найден")
    variant = AbVariant(scenario_id=scenario_id, text=payload.text.strip())
    db.add(variant)
    await db.flush()
    return _out(variant)


@router.patch("/variant/{variant_id}", response_model=AbVariantOut, summary="Вкл/выкл заход")
async def toggle_variant(
    variant_id: uuid.UUID, payload: AbVariantPatch, _user: OperatorUser, db: DbDep
) -> AbVariantOut:
    variant = await db.get(AbVariant, variant_id)
    if variant is None:
        raise NotFoundError("Вариант не найден")
    variant.enabled = payload.enabled
    await db.flush()
    return _out(variant)


@router.delete("/variant/{variant_id}", response_model=Ok, summary="Удалить заход")
async def delete_variant(variant_id: uuid.UUID, _user: OperatorUser, db: DbDep) -> Ok:
    variant = await db.get(AbVariant, variant_id)
    if variant is None:
        raise NotFoundError("Вариант не найден")
    await db.execute(sa_delete(AbVariant).where(AbVariant.id == variant_id))
    return Ok()
