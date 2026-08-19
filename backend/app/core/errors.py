"""Доменные ошибки и централизованная обработка (ТЗ §38).

Каждая ошибка несёт машинный код и HTTP-статус, поэтому обработчику не нужно
разбирать текст сообщения. Наружу отдаётся стабильный JSON-конверт; детали,
пригодные для отладки, уходят только в лог.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm.exc import StaleDataError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger

logger = get_logger(__name__)


class AppError(Exception):
    """База для всех ожидаемых ошибок приложения."""

    code = "internal_error"
    http_status = status.HTTP_500_INTERNAL_SERVER_ERROR
    message = "Internal server error"

    def __init__(self, message: str | None = None, **details: Any) -> None:
        self.message = message or self.message
        self.details = details
        super().__init__(self.message)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        if self.details:
            payload["error"]["details"] = self.details
        return payload


# --- 4xx ------------------------------------------------------------------
class NotFoundError(AppError):
    code = "not_found"
    http_status = status.HTTP_404_NOT_FOUND
    message = "Resource not found"


class ConflictError(AppError):
    code = "conflict"
    http_status = status.HTTP_409_CONFLICT
    message = "Conflicting state"


class InvalidInputError(AppError):
    code = "invalid_input"
    http_status = status.HTTP_422_UNPROCESSABLE_CONTENT
    message = "Invalid input"


class AuthenticationError(AppError):
    code = "unauthenticated"
    http_status = status.HTTP_401_UNAUTHORIZED
    message = "Authentication required"


class PermissionDeniedError(AppError):
    code = "forbidden"
    http_status = status.HTTP_403_FORBIDDEN
    message = "Insufficient permissions"


class RateLimitedError(AppError):
    code = "rate_limited"
    http_status = status.HTTP_429_TOO_MANY_REQUESTS
    message = "Too many requests"


# --- Внешние зависимости ---------------------------------------------------
class ExternalServiceError(AppError):
    code = "external_service_error"
    http_status = status.HTTP_502_BAD_GATEWAY
    message = "External service failed"


class TelegramError(ExternalServiceError):
    code = "telegram_error"
    message = "Telegram request failed"


class TelegramAuthRequiredError(TelegramError):
    code = "telegram_auth_required"
    http_status = status.HTTP_409_CONFLICT
    message = "Telegram account requires re-authentication"


class TelegramFloodWaitError(TelegramError):
    code = "telegram_flood_wait"
    http_status = status.HTTP_429_TOO_MANY_REQUESTS
    message = "Telegram asked to wait before retrying"

    def __init__(self, seconds: int, message: str | None = None) -> None:
        super().__init__(message, seconds=seconds)
        self.seconds = seconds


class AIError(ExternalServiceError):
    code = "ai_error"
    message = "AI provider failed"


class AIBudgetExceededError(AIError):
    code = "ai_budget_exceeded"
    http_status = status.HTTP_429_TOO_MANY_REQUESTS
    message = "AI budget for the period is exhausted"


class AIResponseFormatError(AIError):
    code = "ai_response_format"
    message = "AI returned a response that does not match the expected schema"

    def __init__(self, message: str | None = None, *, raw_text: str = "", **details: Any) -> None:
        super().__init__(message, **details)
        # Исходный текст нужен вызывающему: для ответа человеку схема не
        # обязательна, и терять готовую фразу из-за формата — расточительно.
        self.raw_text = raw_text


# --- Инфраструктура --------------------------------------------------------
class DatabaseError(AppError):
    code = "database_error"
    http_status = status.HTTP_503_SERVICE_UNAVAILABLE
    message = "Database is unavailable"


class RedisError(AppError):
    code = "redis_error"
    http_status = status.HTTP_503_SERVICE_UNAVAILABLE
    message = "Redis is unavailable"


class WorkerUnavailableError(AppError):
    code = "worker_unavailable"
    http_status = status.HTTP_503_SERVICE_UNAVAILABLE
    message = "No worker is currently serving this account"


class CommandTimeoutError(AppError):
    code = "command_timeout"
    http_status = status.HTTP_504_GATEWAY_TIMEOUT
    message = "Worker did not answer in time"


# --- Обработчики -----------------------------------------------------------
MAX_DETAIL_LENGTH = 300


def _short(detail: str) -> str:
    """Ошибки Telethon и драйвера умеют тащить в лог килобайты сырых байтов."""
    return detail if len(detail) <= MAX_DETAIL_LENGTH else detail[:MAX_DETAIL_LENGTH] + "…"


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_request: Request, exc: AppError) -> JSONResponse:
        log = logger.warning if exc.http_status < 500 else logger.error
        log("app_error", code=exc.code, status=exc.http_status, detail=_short(exc.message))
        return JSONResponse(status_code=exc.http_status, content=exc.to_payload())

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "error": {
                    "code": "invalid_input",
                    "message": "Request validation failed",
                    "details": exc.errors(),
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": "http_error", "message": str(exc.detail)}},
        )

    @app.exception_handler(IntegrityError)
    async def _integrity_error(_request: Request, exc: IntegrityError) -> JSONResponse:
        logger.warning("db_integrity_error", detail=_short(str(exc.orig)))
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=ConflictError("Constraint violation").to_payload(),
        )

    @app.exception_handler(StaleDataError)
    async def _stale_error(_request: Request, exc: StaleDataError) -> JSONResponse:
        # Строку изменили или удалили из другой вкладки, пока эта её правила.
        # Это не сбой базы, и сообщать о нём надо иначе.
        logger.info("stale_row", detail=_short(str(exc)))
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=ConflictError(
                "Запись изменилась или была удалена — обновите страницу"
            ).to_payload(),
        )

    @app.exception_handler(SQLAlchemyError)
    async def _db_error(_request: Request, exc: SQLAlchemyError) -> JSONResponse:
        logger.error("db_error", detail=_short(str(exc)))
        return JSONResponse(
            status_code=DatabaseError.http_status, content=DatabaseError().to_payload()
        )

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_error", error_type=type(exc).__name__)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=AppError().to_payload(),
        )
