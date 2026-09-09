"""Просмотр историй (Stories) пользователя — прогрев.

Помечаем активные истории человека просмотренными, чтобы наш аккаунт появился
у него в списке зрителей. Всё делает воркер (владелец клиента). Любая ошибка —
это 0 просмотров, а не падение: прогрев вторичен по отношению к основной работе.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)


async def view_user_stories(client: Any, tg_user_id: int) -> int:
    """Отмечает активные истории пользователя как просмотренные. Возвращает их число."""
    from telethon.tl import functions, types

    try:
        entity = await client.get_entity(tg_user_id)
    except Exception as exc:  # noqa: BLE001 — не резолвится (нет access_hash/приватность)
        logger.debug("story_entity_unresolved", tg_user_id=tg_user_id, detail=str(exc)[:120])
        return 0

    try:
        peer = await client(functions.stories.GetPeerStoriesRequest(peer=entity))
    except Exception as exc:  # noqa: BLE001 — нет историй/приватность/rate limit
        logger.debug("story_fetch_failed", tg_user_id=tg_user_id, detail=str(exc)[:120])
        return 0

    items = getattr(getattr(peer, "stories", None), "stories", None) or []
    ids = [item.id for item in items if isinstance(item, types.StoryItem)]
    if not ids:
        return 0

    try:
        await client(functions.stories.ReadStoriesRequest(peer=entity, max_id=max(ids)))
    except Exception as exc:  # noqa: BLE001 — просмотр не зачёлся, не критично
        logger.debug("story_read_failed", tg_user_id=tg_user_id, detail=str(exc)[:120])
        return 0

    return len(ids)
