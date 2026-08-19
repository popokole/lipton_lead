"""Точка входа API.

Собирает FastAPI-приложение: конфигурация, логирование, ресурсы (PostgreSQL,
Redis), middleware, обработчики ошибок, метрики и роутеры.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator

from app.api.middleware import RequestContextMiddleware
from app.api.router import api_router, root_router
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.runtime import VERSION, Runtime
from app.security.bootstrap import ensure_admin_exists

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    runtime: Runtime = app.state.runtime
    await runtime.startup()
    # Первый администратор нужен, чтобы в панель вообще можно было войти.
    await ensure_admin_exists(runtime.settings, runtime.database)
    try:
        yield
    finally:
        await runtime.shutdown()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title="Telegram AI Automation Platform",
        version=VERSION,
        description=(
            "Панель управления Telegram-аккаунтами: мониторинг чатов, правила, "
            "AI-анализ и автоответы."
        ),
        docs_url="/docs" if not settings.is_production else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not settings.is_production else None,
        lifespan=lifespan,
    )
    app.state.runtime = Runtime(settings)

    app.add_middleware(RequestContextMiddleware)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
            expose_headers=["X-Request-ID"],
        )

    register_exception_handlers(app)

    Instrumentator(
        should_group_status_codes=False,
        excluded_handlers=["/metrics", "/live"],
    ).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

    app.include_router(root_router)
    app.include_router(api_router)

    return app


app = create_app()
