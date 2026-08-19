"""Формат сообщений шины.

Команды и события ходят между процессами, поэтому их структура — публичный
контракт, а не деталь реализации. Pydantic здесь не украшение: он ловит
рассогласование версий API и воркера на границе, а не в середине обработки.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.clock import utcnow


class CommandType(StrEnum):
    """Что API просит сделать воркер, владеющий аккаунтом."""

    PING = "PING"
    CONNECT = "CONNECT"
    DISCONNECT = "DISCONNECT"
    SEND_CODE = "SEND_CODE"
    SIGN_IN = "SIGN_IN"
    SIGN_IN_PASSWORD = "SIGN_IN_PASSWORD"
    LOG_OUT = "LOG_OUT"
    LIST_DIALOGS = "LIST_DIALOGS"
    RESOLVE_CHAT = "RESOLVE_CHAT"
    SEND_MESSAGE = "SEND_MESSAGE"
    ACCOUNT_INFO = "ACCOUNT_INFO"
    CHAT_PHOTO = "CHAT_PHOTO"


class Command(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    type: CommandType
    account_id: uuid.UUID
    # Коды входа и пароли 2FA ходят здесь и нигде не сохраняются: ни в БД,
    # ни в логах — редактор логов вырезает эти ключи по имени.
    payload: dict[str, Any] = Field(default_factory=dict)

    def redacted(self) -> dict[str, Any]:
        """Представление для логов: тип и адресат, без содержимого."""
        return {"command_id": str(self.id), "type": self.type, "account_id": str(self.account_id)}


class CommandResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    command_id: uuid.UUID
    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None

    @classmethod
    def success(cls, command_id: uuid.UUID, **data: Any) -> CommandResult:
        return cls(command_id=command_id, ok=True, data=data)

    @classmethod
    def failure(cls, command_id: uuid.UUID, code: str, message: str) -> CommandResult:
        return cls(command_id=command_id, ok=False, error_code=code, error_message=message)


class EventType(StrEnum):
    """Что происходит в системе — для realtime-обновления панели (ТЗ §27)."""

    ACCOUNT_STATUS = "account.status"
    WORKER_STATUS = "worker.status"
    MESSAGE_NEW = "message.new"
    AI_ANALYSIS = "ai.analysis"
    ACTION_CREATED = "action.created"
    ACTION_SENT = "action.sent"
    LEAD_UPDATED = "lead.updated"
    HUMAN_HANDOFF = "human.handoff"
    ERROR = "error"


class Event(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: EventType
    ts: str = Field(default_factory=lambda: utcnow().isoformat())
    account_id: uuid.UUID | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
