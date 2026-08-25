"""Очередь подтверждения сомнительных ответов в панели.

Оператор видит ожидающие ответы, может поправить текст и отправить или
пропустить. Реальную отправку делает воркер (владеет клиентом Telegram):
одобренные (status=approved) он подхватывает и шлёт.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import desc, select

from app.api.deps import CurrentUser, DbDep, OperatorUser
from app.core.clock import utcnow
from app.core.errors import InvalidInputError, NotFoundError
from app.models import PendingReview
from app.schemas.common import Ok

router = APIRouter(prefix="/reviews", tags=["reviews"])


class ReviewOut(BaseModel):
    id: uuid.UUID
    incoming_text: str | None
    reply_text: str
    dm_text: str | None
    confidence: float | None
    sender: str | None
    created_at: datetime


class ReviewApprove(BaseModel):
    # Опционально поправленные тексты (иначе отправим как есть).
    reply_text: str | None = Field(default=None, max_length=4000)
    dm_text: str | None = Field(default=None, max_length=4000)


def _sender(r: PendingReview) -> str | None:
    if r.sender_display_name:
        return r.sender_display_name
    if r.sender_username:
        return f"@{r.sender_username}"
    return str(r.target_sender_tg_id) if r.target_sender_tg_id else None


@router.get("", response_model=list[ReviewOut], summary="Ожидают подтверждения")
async def list_pending(_user: CurrentUser, db: DbDep) -> list[ReviewOut]:
    rows = await db.scalars(
        select(PendingReview)
        .where(PendingReview.status == "pending")
        .order_by(desc(PendingReview.created_at))
        .limit(200)
    )
    return [
        ReviewOut(
            id=r.id,
            incoming_text=r.incoming_text,
            reply_text=r.reply_text,
            dm_text=r.dm_text,
            confidence=float(r.confidence) if r.confidence is not None else None,
            sender=_sender(r),
            created_at=r.created_at,
        )
        for r in rows.all()
    ]


@router.post("/{review_id}/approve", response_model=Ok, summary="Поправить и отправить")
async def approve(
    review_id: uuid.UUID, payload: ReviewApprove, _user: OperatorUser, db: DbDep
) -> Ok:
    review = await db.get(PendingReview, review_id)
    if review is None:
        raise NotFoundError("Заявка не найдена")
    if review.status != "pending":
        raise InvalidInputError("Уже обработано")
    if payload.reply_text is not None and payload.reply_text.strip():
        review.reply_text = payload.reply_text.strip()
    if payload.dm_text is not None:
        review.dm_text = payload.dm_text.strip() or None
    # Воркер подхватит одобренную заявку и отправит.
    review.status = "approved"
    await db.flush()
    return Ok(detail="Отправляется")


@router.post("/{review_id}/ignore", response_model=Ok, summary="Пропустить")
async def ignore(review_id: uuid.UUID, _user: OperatorUser, db: DbDep) -> Ok:
    review = await db.get(PendingReview, review_id)
    if review is None:
        raise NotFoundError("Заявка не найдена")
    if review.status == "pending":
        review.status = "ignored"
        review.decided_at = utcnow()
        await db.flush()
    return Ok(detail="Пропущено")
