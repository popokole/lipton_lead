"""Просмотр историй (Stories) пользователя — мягкий прогрев.

Помечаем активные истории человека просмотренными, чтобы наш аккаунт появился
у него в зрителях. Всё делает воркер (владелец клиента). Любая ошибка — это ноль
действий, а не падение: прогрев вторичен.

Авто-лайк историй убран намеренно: автоматические реакции с аккаунта —
поведение, за которое Telegram замораживает аккаунт (инцидент 09.2026,
«Фейк основа»). Прогрев теперь только просмотр, без каких-либо записей.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)


async def read_story_stats(redis: Any, tz_offset: int) -> dict[str, Any]:
    """Счётчики авто-просмотра из Redis: сегодня/всего + последние. Для бота и API."""
    import json
    from datetime import timedelta

    from app.core.clock import utcnow

    day = (utcnow() + timedelta(hours=tz_offset)).strftime("%Y-%m-%d")

    async def _int(key: str) -> int:
        return int(await redis.get(key) or 0)

    recent: list[dict[str, Any]] = []
    for raw in await redis.lrange("story:recent", 0, 19):
        try:
            recent.append(json.loads(raw))
        except (ValueError, TypeError):
            continue  # битую запись пропускаем

    return {
        "viewed_today": await _int(f"story:viewed:{day}"),
        "liked_today": await _int(f"story:liked:{day}"),
        "viewed_total": await _int("story:viewed:total"),
        "liked_total": await _int("story:liked:total"),
        "reacted_today": await _int(f"react:{day}"),
        "reacted_total": await _int("react:total"),
        "recent": recent,
    }


def _display_name(entity: Any) -> str:
    parts = [getattr(entity, "first_name", None), getattr(entity, "last_name", None)]
    name = " ".join(p for p in parts if p)
    if name:
        return name
    username = getattr(entity, "username", None)
    return f"@{username}" if username else ""


async def engage_user_stories(
    client: Any, tg_user_id: int
) -> tuple[int, str, str | None]:
    """Помечает активные истории пользователя просмотренными (без лайка).

    Возвращает (сколько просмотрено, имя, @username|None).
    """
    from telethon.tl import functions, types

    name = str(tg_user_id)
    username: str | None = None
    try:
        entity = await client.get_entity(tg_user_id)
        name = _display_name(entity) or name
        username = getattr(entity, "username", None)
    except Exception as exc:  # noqa: BLE001 — не резолвится (нет access_hash/приватность)
        logger.debug("story_entity_unresolved", tg_user_id=tg_user_id, detail=str(exc)[:120])
        return 0, name, username

    try:
        peer = await client(functions.stories.GetPeerStoriesRequest(peer=entity))
    except Exception as exc:  # noqa: BLE001 — нет историй/приватность/rate limit
        logger.debug("story_fetch_failed", tg_user_id=tg_user_id, detail=str(exc)[:120])
        return 0, name, username

    items = getattr(getattr(peer, "stories", None), "stories", None) or []
    ids = [item.id for item in items if isinstance(item, types.StoryItem)]
    if not ids:
        return 0, name, username

    latest = max(ids)
    try:
        await client(functions.stories.ReadStoriesRequest(peer=entity, max_id=latest))
    except Exception as exc:  # noqa: BLE001 — просмотр не зачёлся
        logger.debug("story_read_failed", tg_user_id=tg_user_id, detail=str(exc)[:120])
        return 0, name, username

    return len(ids), name, username


