"""Единый реестр ключей Redis.

Ключи собираются только здесь. Строковые литералы, разбросанные по коду, — это
гарантированный рассинхрон между тем, кто пишет, и тем, кто читает.
"""

from __future__ import annotations

from uuid import UUID

NAMESPACE = "tgai"


def _key(*parts: object) -> str:
    return ":".join([NAMESPACE, *(str(part) for part in parts)])


# --- Воркеры ---------------------------------------------------------------
def worker_heartbeat(worker_id: UUID | str) -> str:
    return _key("worker", worker_id, "heartbeat")


WORKER_INDEX = _key("workers")


# --- Аренда аккаунтов ------------------------------------------------------
def account_lease(account_id: UUID | str) -> str:
    return _key("lease", "account", account_id)


# --- Дедупликация ----------------------------------------------------------
def message_claim(account_id: UUID | str, chat_id: int, message_id: int) -> str:
    return _key("claim", account_id, chat_id, message_id)


# --- Cooldown --------------------------------------------------------------
def cooldown(scope: str, *parts: object) -> str:
    return _key("cooldown", scope, *parts)


# --- Шина команд и событий -------------------------------------------------
def command_stream(worker_id: UUID | str) -> str:
    return _key("commands", worker_id)


def command_reply(correlation_id: UUID | str) -> str:
    return _key("commands", "reply", correlation_id)


EVENTS_CHANNEL = _key("events")


# --- Бюджет AI -------------------------------------------------------------
def ai_budget(day: str) -> str:
    return _key("ai", "budget", day)


AI_CONCURRENCY = _key("ai", "concurrency")
