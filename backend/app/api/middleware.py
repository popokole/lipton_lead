"""HTTP-middleware: контекст запроса и access-лог."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import bind_request_context, clear_request_context, get_logger

logger = get_logger("http")

REQUEST_ID_HEADER = "X-Request-ID"

# Пути, которые опрашиваются постоянно, — их access-лог только зашумляет вывод.
_QUIET_PATHS = frozenset({"/live", "/health", "/ready", "/metrics"})


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        request.state.request_id = request_id

        clear_request_context()
        bind_request_context(request_id=request_id)

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = int((time.perf_counter() - started) * 1000)
            logger.exception(
                "request_failed",
                method=request.method,
                path=request.url.path,
                duration_ms=duration_ms,
            )
            raise
        finally:
            clear_request_context()

        duration_ms = int((time.perf_counter() - started) * 1000)
        response.headers[REQUEST_ID_HEADER] = request_id

        if request.url.path not in _QUIET_PATHS:
            logger.info(
                "request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=duration_ms,
            )
        return response
