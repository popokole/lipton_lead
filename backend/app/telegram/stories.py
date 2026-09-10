"""Просмотр и лайк историй (Stories) пользователя — прогрев.

Помечаем активные истории человека просмотренными и ставим реакцию (❤️), чтобы
наш аккаунт появился у него в зрителях и лайках. Всё делает воркер (владелец
клиента). Любая ошибка — это ноль действий, а не падение: прогрев вторичен.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

# Telegram-лайк истории — красное сердце. Разные клиенты шлют его то с
# вариацией U+FE0F, то без — пробуем оба и проверяем, что реакция применилась.
LIKE_EMOJIS = ["❤", "❤️"]


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
    client: Any, tg_user_id: int, *, like: bool = True
) -> tuple[int, bool, str, str | None]:
    """Смотрит (и лайкает) активные истории пользователя.

    Возвращает (сколько просмотрено, поставлен ли лайк, имя, @username|None).
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
        return 0, False, name, username

    try:
        peer = await client(functions.stories.GetPeerStoriesRequest(peer=entity))
    except Exception as exc:  # noqa: BLE001 — нет историй/приватность/rate limit
        logger.debug("story_fetch_failed", tg_user_id=tg_user_id, detail=str(exc)[:120])
        return 0, False, name, username

    items = getattr(getattr(peer, "stories", None), "stories", None) or []
    ids = [item.id for item in items if isinstance(item, types.StoryItem)]
    if not ids:
        return 0, False, name, username

    latest = max(ids)
    try:
        await client(functions.stories.ReadStoriesRequest(peer=entity, max_id=latest))
    except Exception as exc:  # noqa: BLE001 — просмотр не зачёлся
        logger.debug("story_read_failed", tg_user_id=tg_user_id, detail=str(exc)[:120])
        return 0, False, name, username

    liked = False
    if like:
        for emo in LIKE_EMOJIS:
            try:
                await client(
                    functions.stories.SendReactionRequest(
                        peer=entity,
                        story_id=latest,
                        reaction=types.ReactionEmoji(emoticon=emo),
                        add_to_recent=True,
                    )
                )
            except Exception as exc:  # noqa: BLE001, PERF203 — этот вариант не подошёл
                logger.debug("story_like_send_failed", detail=str(exc)[:120], emoji=emo)
                continue
            # Проверяем, что реакция реально применилась (иначе логировали бы
            # ложное «поставлено»): перечитываем историю и смотрим sent_reaction.
            if await _reaction_applied(client, functions, types, entity, latest):
                liked = True
                break

    return len(ids), liked, name, username


async def _reaction_applied(
    client: Any, functions: Any, types: Any, entity: Any, story_id: int
) -> bool:
    """True, если у истории story_id стоит наша реакция."""
    try:
        peer = await client(functions.stories.GetPeerStoriesRequest(peer=entity))
    except Exception:  # noqa: BLE001 — не смогли проверить
        return False
    for item in getattr(getattr(peer, "stories", None), "stories", None) or []:
        if isinstance(item, types.StoryItem) and item.id == story_id:
            return getattr(item, "sent_reaction", None) is not None
    return False
