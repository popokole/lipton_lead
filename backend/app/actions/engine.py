"""Движок действий (ТЗ §18).

Обработчики регистрируются в реестре по типу действия. Ветвление `if action ==
...` на сотни строк здесь невозможно по построению: добавить действие — значит
написать обработчик и зарегистрировать его.

Каждое действие сначала записывается в базу со статусом PENDING и только потом
выполняется. `dedup_key` делает выполнение идемпотентным: повторная попытка
после сбоя сети не отправит второе сообщение в чат.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.clock import utcnow
from app.core.logging import get_logger
from app.database.session import Database
from app.models import Action, ActionStatus, ActionType
from app.telegram.messages import NormalizedMessage

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ActionRequest:
    """Что и от чьего имени нужно сделать."""

    type: ActionType
    account_id: uuid.UUID
    dedup_key: str
    message: NormalizedMessage | None = None
    chat_id: uuid.UUID | None = None
    message_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    rule_id: uuid.UUID | None = None
    scenario_id: uuid.UUID | None = None
    reply_text: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ActionResult:
    status: ActionStatus
    action_id: uuid.UUID | None = None
    detail: str | None = None
    sent_tg_message_id: int | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is ActionStatus.SENT


class ActionHandler(Protocol):
    """Обработчик одного типа действия."""

    async def execute(self, request: ActionRequest, action_id: uuid.UUID) -> ActionResult: ...


class ActionEngine:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._handlers: dict[ActionType, ActionHandler] = {}

    def register(self, action_type: ActionType, handler: ActionHandler) -> None:
        if action_type in self._handlers:
            raise ValueError(f"handler for {action_type} is already registered")
        self._handlers[action_type] = handler

    @property
    def registered(self) -> frozenset[ActionType]:
        return frozenset(self._handlers)

    async def dispatch(self, request: ActionRequest) -> ActionResult:
        handler = self._handlers.get(request.type)
        if handler is None:
            logger.error("action_handler_missing", action=request.type.value)
            return ActionResult(
                status=ActionStatus.FAILED, detail=f"no handler for {request.type.value}"
            )

        action_id, already_done = await self._persist(request)
        if already_done:
            # Такое действие уже выполнялось: повтор не нужен и вреден.
            logger.info("action_already_executed", action_id=str(action_id))
            return ActionResult(status=ActionStatus.SENT, action_id=action_id, detail="duplicate")

        try:
            result = await handler.execute(request, action_id)
        except Exception as exc:
            logger.exception("action_failed", action=request.type.value)
            await self._finish(action_id, ActionStatus.FAILED, error=f"{type(exc).__name__}: {exc}")
            return ActionResult(status=ActionStatus.FAILED, action_id=action_id, detail=str(exc))

        await self._finish(
            action_id,
            result.status,
            error=result.detail if result.status is ActionStatus.FAILED else None,
            sent_tg_message_id=result.sent_tg_message_id,
        )
        return ActionResult(
            status=result.status,
            action_id=action_id,
            detail=result.detail,
            sent_tg_message_id=result.sent_tg_message_id,
        )

    async def _persist(self, request: ActionRequest) -> tuple[uuid.UUID, bool]:
        """Заводит запись действия. Второй элемент — «уже выполнялось»."""
        async with self._database.session() as db:
            action = Action(
                type=request.type,
                status=ActionStatus.PENDING,
                account_id=request.account_id,
                chat_id=request.chat_id,
                message_id=request.message_id,
                conversation_id=request.conversation_id,
                rule_id=request.rule_id,
                scenario_id=request.scenario_id,
                payload=dict(request.payload),
                reply_text=request.reply_text,
                validation=request.validation,
                dedup_key=request.dedup_key,
                scheduled_at=utcnow(),
            )
            db.add(action)
            try:
                await db.flush()
            except IntegrityError:
                await db.rollback()
                existing = await db.scalar(
                    select(Action).where(Action.dedup_key == request.dedup_key)
                )
                if existing is None:
                    raise
                return existing.id, existing.status in {
                    ActionStatus.SENT,
                    ActionStatus.SENDING,
                }
            return action.id, False

    async def _finish(
        self,
        action_id: uuid.UUID,
        status: ActionStatus,
        *,
        error: str | None = None,
        sent_tg_message_id: int | None = None,
    ) -> None:
        async with self._database.session() as db:
            action = await db.get(Action, action_id)
            if action is None:
                return
            action.status = status
            action.attempts += 1
            action.error = error
            if status is ActionStatus.SENT:
                action.sent_at = utcnow()
                action.sent_tg_message_id = sent_tg_message_id
