"""Список и выгрузка файлов из чата (группа, личка, канал) от имени аккаунта.

Нужна, чтобы забрать файлы, которые человек прислал аккаунту: работы для
портфолио, скриншоты, видео. Только чтение: в чат ничего не отправляется.
Файлы сохраняются в папку воркера MEDIA_DIR/<id чата>/, оттуда их забирают
через `docker compose cp worker:...`. В лог уходят только id и размеры.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from app.core.errors import InvalidInputError
from app.core.logging import get_logger
from app.telegram.channel_posts import (
    _flood_waits_surface,
    _FloodBudget,
    _history_batch,
    _int_field,
    _telegram_call,
    channel_reference,
    describe_chat,
    media_type,
    resolve_chat_entity,
    telegram_error,
    telegram_unavailable,
)

logger = get_logger(__name__)

MEDIA_DIR = Path("/tmp/chat_media")
CHAT_MEDIA_LIMIT_MAX = 200
DEFAULT_CHAT_MEDIA_LIMIT = 50
MAX_FILE_BYTES = 200 * 1024 * 1024
_SKIP_KINDS = frozenset({"webpage", "poll", "geo", "venue", "contact", "dice", "game"})

Sleep = Any


@dataclass(frozen=True, slots=True)
class ChatMediaRequest:
    chat: str | int
    limit: int = DEFAULT_CHAT_MEDIA_LIMIT
    download: bool = False
    min_id: int = 0


def parse_chat_media_payload(payload: dict[str, Any]) -> ChatMediaRequest:
    """READ_CHAT_MEDIA: {chat, limit ≤ 200, download = false, min_id = 0}.

    Смотрит последние limit сообщений чата; min_id отсекает всё, что было
    до него (id ≤ min_id)."""
    chat = channel_reference(payload.get("chat"))
    limit = _int_field(payload, "limit", DEFAULT_CHAT_MEDIA_LIMIT, low=1, high=CHAT_MEDIA_LIMIT_MAX)
    min_id = _int_field(payload, "min_id", 0, low=0, high=2**31 - 1)
    download = payload.get("download", False)
    if not isinstance(download, bool):
        raise InvalidInputError("Field 'download' must be a boolean")
    return ChatMediaRequest(chat=chat, limit=limit, download=download, min_id=min_id)


def _file_info(message: Any) -> tuple[str | None, int | None]:
    file = getattr(message, "file", None)
    if file is None:
        return None, None
    name = getattr(file, "name", None)
    size = getattr(file, "size", None)
    return (name if isinstance(name, str) else None), (size if isinstance(size, int) else None)


async def list_chat_media(
    client: Any,
    request: ChatMediaRequest,
    *,
    sleep: Sleep = asyncio.sleep,
    media_dir: Path = MEDIA_DIR,
) -> dict[str, Any]:
    """Сообщения с медиа от старых к новым; с download — ещё и файлы на диск воркера."""
    from telethon.errors import RPCError

    budget = _FloodBudget(sleep)
    with _flood_waits_surface(client):
        entity = await budget.run(
            partial(resolve_chat_entity, client, request.chat), step="resolve"
        )
        messages = await _telegram_call(
            budget,
            partial(_history_batch, client, entity, request.limit, 0),
            step="history",
        )

    chat = describe_chat(entity)
    target = media_dir / str(abs(int(chat["id"] or 0)))
    items: list[dict[str, Any]] = []
    for message in sorted(messages, key=lambda m: int(m.id)):
        if request.min_id and int(message.id) <= request.min_id:
            continue
        kind = media_type(message)
        if kind is None or kind in _SKIP_KINDS:
            continue
        name, size = _file_info(message)
        date = getattr(message, "date", None)
        item: dict[str, Any] = {
            "id": int(message.id),
            "date": date.isoformat() if date is not None else None,
            "kind": kind,
            "grouped_id": getattr(message, "grouped_id", None),
            "sender_id": getattr(message, "sender_id", None),
            "name": name,
            "size": size,
            "text": (getattr(message, "message", None) or "")[:1000],
        }
        if request.download:
            if size is not None and size > MAX_FILE_BYTES:
                item["skipped"] = "too_large"
            else:
                target.mkdir(parents=True, exist_ok=True)
                try:
                    # Без расширения в пути Telethon сам допишет правильное.
                    path = await client.download_media(message, file=str(target / str(message.id)))
                except RPCError as exc:
                    raise telegram_error(exc) from None
                except ValueError as exc:
                    raise telegram_unavailable(exc) from None
                item["file"] = str(path) if path else None
        items.append(item)

    logger.info(
        "chat_media_listed",
        tg_chat_id=chat["id"],
        messages=len(messages),
        media=len(items),
        downloaded=sum(1 for item in items if item.get("file")),
    )
    return {"chat": chat, "items": items, "dir": str(target) if request.download else None}
