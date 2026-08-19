"""Кеш собеседников Telegram.

Чтобы написать в чат, Telethon нужен не только его идентификатор, но и
`access_hash` — числовой пропуск, который выдаёт сам Telegram. Обычно клиент
накапливает их в файле сессии по мере работы, но мы собираем сессию из tdata,
и кеш сущностей туда не переносится. Отправка по «голому» ID падает с
`Could not find the input entity`.

Решение простое: каждое входящее сообщение уже содержит готовый peer с
пропуском. Запоминаем его при получении — и ответ отправляем по нему.

Кеш живёт в памяти воркера и ограничен по размеру: мониторинг сотни чатов не
должен превращаться в утечку. Потеря кеша при перезапуске не страшна — он
наполнится заново с первым же входящим сообщением.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

DEFAULT_CAPACITY = 2000


class PeerCache:
    """Ограниченный по размеру кеш «чат → peer» в разрезе аккаунта."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._capacity = capacity
        self._items: OrderedDict[tuple[uuid.UUID, int], Any] = OrderedDict()

    def remember(self, account_id: uuid.UUID, tg_chat_id: int, peer: Any) -> None:
        if peer is None:
            return
        key = (account_id, tg_chat_id)
        self._items[key] = peer
        self._items.move_to_end(key)
        while len(self._items) > self._capacity:
            self._items.popitem(last=False)

    def get(self, account_id: uuid.UUID, tg_chat_id: int) -> Any | None:
        key = (account_id, tg_chat_id)
        peer = self._items.get(key)
        if peer is not None:
            self._items.move_to_end(key)
        return peer

    def forget_account(self, account_id: uuid.UUID) -> None:
        for key in [key for key in self._items if key[0] == account_id]:
            del self._items[key]

    def __len__(self) -> int:
        return len(self._items)


async def extract_input_peer(event: Any) -> Any | None:
    """Достаёт peer из события Telethon, ничего не спрашивая у сети.

    `input_chat` — уже готовый объект с access_hash, если Telethon получил его
    вместе с обновлением. Асинхронный вариант вызываем только как запасной: он
    умеет сходить в сеть, а обработчик сообщения задерживать нельзя.
    """
    peer = getattr(event, "input_chat", None)
    if peer is not None:
        return peer

    getter = getattr(event, "get_input_chat", None)
    if getter is None:
        return None
    try:
        return await getter()
    except Exception as exc:  # noqa: BLE001 — без peer просто отправим по id
        logger.debug("input_peer_unavailable", detail=str(exc))
        return None
