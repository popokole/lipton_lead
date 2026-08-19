"""Realtime-канал панели (ТЗ §27).

События рождаются в воркере, а браузер подключён к произвольной реплике API.
Поэтому каждая реплика подписана на общий канал Redis и раздаёт события своим
сокетам: без этого панель видела бы только то, что произошло «на её» реплике.

Подписка на Redis одна на процесс и живёт, пока есть хотя бы один клиент.
Токен передаётся параметром запроса — заголовки в браузерном WebSocket API
задать нельзя.
"""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.bus.events import EventSubscriber
from app.core.errors import AuthenticationError
from app.core.logging import get_logger
from app.core.runtime import Runtime
from app.security.tokens import decode_token

logger = get_logger(__name__)

router = APIRouter()


class ConnectionHub:
    """Держит открытые сокеты и рассылает им события."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._pump: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @property
    def size(self) -> int:
        return len(self._clients)

    async def attach(self, websocket: WebSocket, runtime: Runtime) -> None:
        async with self._lock:
            self._clients.add(websocket)
            if self._pump is None or self._pump.done():
                self._pump = asyncio.create_task(self._forward(runtime), name="ws-pump")

    async def detach(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(websocket)
            if not self._clients and self._pump is not None:
                self._pump.cancel()
                self._pump = None

    async def broadcast(self, payload: str) -> None:
        dead: list[WebSocket] = []
        for client in list(self._clients):
            try:
                if client.client_state is not WebSocketState.CONNECTED:
                    dead.append(client)
                    continue
                await client.send_text(payload)
            except Exception:  # noqa: BLE001 — один отвалившийся не мешает другим
                dead.append(client)

        for client in dead:
            await self.detach(client)

    async def _forward(self, runtime: Runtime) -> None:
        subscriber = EventSubscriber(runtime.redis.client)
        try:
            async for event in subscriber.stream():
                await self.broadcast(event.model_dump_json())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — панель не должна ронять API
            logger.warning("ws_pump_failed", detail=str(exc))


hub = ConnectionHub()


@router.websocket("/ws")
async def events_socket(websocket: WebSocket, token: str = Query(default="")) -> None:
    runtime: Runtime = websocket.app.state.runtime

    try:
        decode_token(runtime.settings, token, "access")
    except AuthenticationError:
        # 1008 = policy violation: клиенту понятно, что дело в токене.
        await websocket.close(code=1008, reason="invalid token")
        return

    await websocket.accept()
    await hub.attach(websocket, runtime)
    logger.info("ws_client_connected", clients=hub.size)

    try:
        while True:
            # Панель ничего не присылает; чтение нужно, чтобы заметить разрыв.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.debug("ws_client_error", detail=str(exc))
    finally:
        await hub.detach(websocket)
        with contextlib.suppress(Exception):
            await websocket.close()
        logger.info("ws_client_disconnected", clients=hub.size)
