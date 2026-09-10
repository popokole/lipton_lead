"""Авто-реакция на сообщение в чате — мягкий прогрев/охват.

Ставит эмодзи-реакцию на свежее сообщение. Пробует несколько эмодзи: в разных
чатах доступный набор реакций отличается. Любая ошибка — это «не поставили», а
не падение воркера.
"""

from __future__ import annotations

import random
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

# Позитивные реакции. Красное сердце — без вариации U+FE0F (иначе Telegram не
# применяет, см. историю с лайками сторис).
REACT_EMOJIS = ["👍", "❤", "🔥", "😁", "🥰", "👏"]


async def react_to_message(client: Any, tg_chat_id: int, tg_message_id: int) -> str | None:
    """Ставит реакцию на сообщение. Возвращает поставленный эмодзи или None."""
    from telethon.tl import functions, types

    try:
        entity = await client.get_entity(tg_chat_id)
    except Exception as exc:  # noqa: BLE001 — чат не резолвится
        logger.debug("react_entity_unresolved", chat=tg_chat_id, detail=str(exc)[:120])
        return None

    for emo in random.sample(REACT_EMOJIS, len(REACT_EMOJIS)):
        try:
            await client(
                functions.messages.SendReactionRequest(
                    peer=entity,
                    msg_id=tg_message_id,
                    reaction=[types.ReactionEmoji(emoticon=emo)],
                    add_to_recent=True,
                )
            )
            return emo
        except Exception as exc:  # noqa: BLE001 — этот эмодзи не подошёл или нет прав
            detail = str(exc)
            # Реакция не входит в разрешённые чатом — пробуем следующий эмодзи.
            if "REACTION_INVALID" in detail or "REACTION_" in detail:
                continue
            # Сообщение удалено / чат закрыт / нет прав — дальше нет смысла.
            logger.debug("react_failed", chat=tg_chat_id, msg=tg_message_id, detail=detail[:120])
            return None
    return None
