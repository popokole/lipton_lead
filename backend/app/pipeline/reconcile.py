"""Довыгрузка пропущенных сообщений (ТЗ §6).

Telethon держит catch_up на server-side «дельте» по pts. Telegram периодически
сам роняет эту синхронизацию внутренней ошибкой (GetChannelDifferenceRequest) —
известная и не чинимая на клиенте проблема на стороне самого Telegram (см.
github.com/LonamiWebs/Telethon issue #4442, закрыт как «not planned»): часть
сообщений из окна сбоя не долетает вообще — ни в базу, ни в конвейер (найдено
2026-08-23: 43 таких сбоя за 6 часов, реальные пропуски по 8-75 сообщений
подряд в нескольких чатах).

iter_messages идёт другим путём — обычная выгрузка истории чата, а не
pts-дельта — и от того же сбоя не зависит. Раз в reconcile_interval_seconds
довыгружаем последние сообщения недавно активных чатов и прогоняем через тот
же MonitorPipeline.handle_event, что и живые события:
  - claim() внутри уже идемпотентен — уже виденные сообщения просто не
    пройдут повторно, дублей можно не бояться;
  - REPLY_MAX_AGE_SECONDS в monitor_pipeline не даст ответить на устаревший
    бэклог как на свежее сообщение — это и старое, и новое поведение
    работает одинаково, независимо от источника события.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from sqlalchemy import select

from app.core.clock import utcnow
from app.core.logging import get_logger
from app.database.session import Database
from app.models import Chat, ProcessedStatus
from app.pipeline.monitor_pipeline import MonitorPipeline
from app.rules.filters import SELF_GUARD_REASONS
from app.telegram.client import TelegramClientLike

logger = get_logger(__name__)

_ALREADY_CLAIMED = "already claimed"
#: SKIPPED-причины, которые означают «в базу не попало и не должно было» —
#: то есть НЕ восстановление пропущенного. И already-claimed, и собственные
#: исходящие сообщения бота: последних в истории chat'а полно (это же и есть
#: его собственные ответы), но SelfGuard отбрасывает их до claim(), так что
#: их reason не "already claimed" — без этого списка они бы задвоили счётчик.
_NOT_RECOVERED_REASONS = _ALREADY_CLAIMED, *SELF_GUARD_REASONS


@dataclass(frozen=True, slots=True)
class _HistoryEvent:
    """Обёртка «сырого» Message из iter_messages под интерфейс normalize().

    MessageNormalizer ждёт event.message — вложенный объект сообщения, как у
    NewMessage.Event. У самого Message .message — это ТЕКСТ (одноимённое поле
    API), а не вложенный объект: без обёртки normalize() принял бы текст за
    сообщение и упал на «message has no message id». Остальные атрибуты
    (chat_id, is_private/is_group, sender_id...) Telethon даёт на самом
    Message теми же именами, что и на событии, — их просто прокидываем как есть.
    """

    message: Any

    def __getattr__(self, name: str) -> Any:
        return getattr(self.message, name)


class ClientLookup(Protocol):
    """То немногое, что нужно от ClientManager — узкий протокол ради тестов
    без полного жизненного цикла подключения."""

    def get(self, account_id: uuid.UUID) -> TelegramClientLike | None: ...


class ChatReconciler:
    def __init__(
        self,
        database: Database,
        clients: ClientLookup,
        pipeline: MonitorPipeline,
        *,
        lookback_messages: int = 30,
        active_within_minutes: int = 30,
    ) -> None:
        self._database = database
        self._clients = clients
        self._pipeline = pipeline
        self._lookback_messages = lookback_messages
        self._active_within = active_within_minutes

    async def run(self, account_id: uuid.UUID) -> int:
        if self._clients.get(account_id) is None:
            return 0

        recovered = 0
        for chat in await self._active_chats(account_id):
            try:
                recovered += await self._reconcile_chat(account_id, chat)
            except Exception as exc:  # noqa: BLE001 — один битый чат не должен рвать проход по остальным
                logger.warning(
                    "reconcile_chat_failed",
                    account_id=str(account_id),
                    tg_chat_id=chat.tg_chat_id,
                    detail=str(exc),
                )

        if recovered:
            logger.info("reconcile_recovered", account_id=str(account_id), count=recovered)
        return recovered

    async def _active_chats(self, account_id: uuid.UUID) -> list[Chat]:
        cutoff = utcnow() - timedelta(minutes=self._active_within)
        async with self._database.session() as db:
            rows = await db.scalars(
                select(Chat).where(Chat.account_id == account_id, Chat.last_message_at >= cutoff)
            )
            return list(rows.all())

    async def _reconcile_chat(self, account_id: uuid.UUID, chat: Chat) -> int:
        client = self._clients.get(account_id)
        if client is None:
            return 0
        peer = self._pipeline.peers.get(account_id, chat.tg_chat_id) or chat.tg_chat_id

        # От старого к новому: в обратном порядке cooldown занял бы самое
        # свежее сообщение первым, и по-настоящему первому пропущенному ответ
        # уже не ушёл бы.
        messages = [
            message
            async for message in client.iter_messages(peer, limit=self._lookback_messages)
        ]
        messages.reverse()

        recovered = 0
        for message in messages:
            outcome = await self._pipeline.handle_event(account_id, _HistoryEvent(message))
            already_known = (
                outcome.status is ProcessedStatus.SKIPPED
                and outcome.reason in _NOT_RECOVERED_REASONS
            )
            if not already_known:
                recovered += 1
        return recovered
