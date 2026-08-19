"""Сборка REST-роутеров (ТЗ §26)."""

from __future__ import annotations

from fastapi import APIRouter

from app.api import ws
from app.api.v1 import accounts, activity, auth, catalog, health, imports, proxies, system

root_router = APIRouter()
root_router.include_router(health.router)
root_router.include_router(ws.router)

api_router = APIRouter(prefix="/api")
api_router.include_router(auth.router)
api_router.include_router(accounts.router)
api_router.include_router(imports.router)
api_router.include_router(proxies.router)
api_router.include_router(catalog.chats_router)
api_router.include_router(catalog.scenarios_router)
api_router.include_router(catalog.rules_router)
api_router.include_router(activity.messages_router)
api_router.include_router(activity.actions_router)
api_router.include_router(activity.logs_router)
api_router.include_router(activity.conversations_router)
api_router.include_router(activity.leads_router)
api_router.include_router(system.workers_router)
api_router.include_router(system.analytics_router)
