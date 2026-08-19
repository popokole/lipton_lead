"""Шина событий воркер → API → WebSocket.

Событие рождается в воркере, а браузер подключён к произвольной реплике API.
Pub/Sub решает именно это: каждая реплика подписана на общий канал и
раздаёт события своим сокетам.

Здесь допустима потеря: события — это обновление картинки в панели, а не
источник истины. Всё, что должно пережить перезапуск, лежит в PostgreSQL.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from redis.asyncio import Redis

from app.bus import keys
from app.bus.messages import Event
from app.core.logging import get_logger

logger = get_logger(__name__)


class EventPublisher:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def publish(self, event: Event) -> None:
        try:
            await self._redis.publish(keys.EVENTS_CHANNEL, event.model_dump_json())
        except Exception as exc:  # noqa: BLE001
            # Обработка сообщения не должна падать из-за недоступной панели.
            logger.warning("event_publish_failed", event_type=event.type, detail=str(exc))


class EventSubscriber:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def stream(self) -> AsyncIterator[Event]:
        pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
        await pubsub.subscribe(keys.EVENTS_CHANNEL)
        try:
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message is None:
                    await asyncio.sleep(0)
                    continue
                try:
                    yield Event.model_validate_json(message["data"])
                except ValueError as exc:
                    logger.warning("event_unparsable", detail=str(exc))
        finally:
            await pubsub.unsubscribe(keys.EVENTS_CHANNEL)
            await pubsub.aclose()
