"""Управление подключениями Telegram-аккаунтов (ТЗ §5).

Главное требование — изоляция. Каждый аккаунт живёт в собственной задаче со
своим клиентом, и любая его ошибка (сеть, слетевшая сессия, FloodWait) не
выходит за пределы этой задачи. Падение одного аккаунта не должно ни
останавливать остальных, ни ронять воркер.

Переподключение — экспоненциальная пауза с потолком. Но не на все ошибки:
если Telegram говорит, что сессия недействительна, повторять бессмысленно —
аккаунт переводится в AUTH_REQUIRED и ждёт человека.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.clock import utcnow
from app.core.config import Settings
from app.core.logging import get_logger
from app.models import AccountStatus
from app.telegram.client import ClientFactory, EventHandler, TelegramClientLike
from app.telegram.session_manager import SessionCredentials

logger = get_logger(__name__)

StatusCallback = Callable[[uuid.UUID, AccountStatus, str | None], Awaitable[None]]
# Telethon передаёт в обработчик только событие, без указания клиента, поэтому
# принадлежность к аккаунту закрывается фабрикой на этапе регистрации.
HandlerFactory = Callable[[uuid.UUID], EventHandler]

# Ошибки, после которых переподключаться бесполезно: нужна повторная
# авторизация человеком.
_FATAL_AUTH_ERRORS = frozenset(
    {
        "AuthKeyUnregisteredError",
        "AuthKeyDuplicatedError",
        "SessionRevokedError",
        "SessionExpiredError",
        "UserDeactivatedError",
        "UserDeactivatedBanError",
        "UnauthorizedError",
    }
)


@dataclass
class ClientHealth:
    account_id: uuid.UUID
    status: AccountStatus
    connected: bool = False
    authorized: bool = False
    last_error: str | None = None
    connect_attempts: int = 0
    connected_at: Any = None


@dataclass
class _Entry:
    account_id: uuid.UUID
    credentials: SessionCredentials
    proxy: dict[str, Any] | None
    health: ClientHealth
    client: TelegramClientLike | None = None
    task: asyncio.Task[None] | None = None
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    ready: asyncio.Event = field(default_factory=asyncio.Event)


class ClientManager:
    def __init__(
        self,
        settings: Settings,
        factory: ClientFactory,
        *,
        on_status: StatusCallback | None = None,
        handler_factory: HandlerFactory | None = None,
    ) -> None:
        self._settings = settings
        self._factory = factory
        self._on_status = on_status
        self._handler_factory = handler_factory
        self._entries: dict[uuid.UUID, _Entry] = {}

    # --- состав ------------------------------------------------------------
    @property
    def account_ids(self) -> list[uuid.UUID]:
        return list(self._entries)

    def get(self, account_id: uuid.UUID) -> TelegramClientLike | None:
        entry = self._entries.get(account_id)
        return entry.client if entry else None

    def health(self, account_id: uuid.UUID) -> ClientHealth | None:
        entry = self._entries.get(account_id)
        return entry.health if entry else None

    def all_health(self) -> list[ClientHealth]:
        return [entry.health for entry in self._entries.values()]

    # --- жизненный цикл ----------------------------------------------------
    async def start(
        self,
        account_id: uuid.UUID,
        credentials: SessionCredentials,
        proxy: dict[str, Any] | None = None,
    ) -> bool:
        """Ставит аккаунт на обслуживание.

        Возвращает False, если клиент уже поднят: вызывающему важно знать, что
        его настройки НЕ применились — молчаливый пропуск здесь однажды уже
        стоил долгих поисков.
        """
        if account_id in self._entries:
            logger.debug("client_already_running", account_id=str(account_id))
            return False

        entry = _Entry(
            account_id=account_id,
            credentials=credentials,
            proxy=proxy,
            health=ClientHealth(account_id=account_id, status=AccountStatus.OFFLINE),
        )
        self._entries[account_id] = entry
        entry.task = asyncio.create_task(self._supervise(entry), name=f"tg-client-{account_id}")
        return True

    async def wait_ready(self, account_id: uuid.UUID, timeout_seconds: float) -> bool:
        """Ждёт первой попытки подключения — нужно командам сразу после старта."""
        entry = self._entries.get(account_id)
        if entry is None:
            return False
        try:
            await asyncio.wait_for(entry.ready.wait(), timeout=timeout_seconds)
        except TimeoutError:
            return False
        return entry.health.status is AccountStatus.ONLINE

    async def stop(self, account_id: uuid.UUID, *, logout: bool = False) -> None:
        entry = self._entries.pop(account_id, None)
        if entry is None:
            return

        entry.stop.set()
        if entry.task is not None:
            entry.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await entry.task

        if entry.client is not None:
            try:
                if logout:
                    await entry.client.log_out()
                await entry.client.disconnect()
            except Exception as exc:  # noqa: BLE001 — остановка идёт до конца
                logger.warning(
                    "client_disconnect_failed", account_id=str(account_id), detail=str(exc)
                )

        await self._set_status(entry, AccountStatus.OFFLINE, None)

    async def shutdown(self) -> None:
        await asyncio.gather(
            *(self.stop(account_id) for account_id in list(self._entries)),
            return_exceptions=True,
        )

    # --- супервизор --------------------------------------------------------
    async def _supervise(self, entry: _Entry) -> None:
        """Держит аккаунт подключённым, пока его не попросят остановиться."""
        delay = self._settings.telegram_reconnect_base_delay

        while not entry.stop.is_set():
            try:
                await self._connect_once(entry)
                delay = self._settings.telegram_reconnect_base_delay
                await self._wait_until_disconnected(entry)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — изоляция ошибок аккаунта
                fatal = isinstance(exc, _AuthorisationLostError) or (
                    type(exc).__name__ in _FATAL_AUTH_ERRORS
                )
                entry.health.last_error = f"{type(exc).__name__}: {exc}"
                entry.ready.set()

                if fatal:
                    logger.error(
                        "account_auth_required",
                        account_id=str(entry.account_id),
                        error_type=type(exc).__name__,
                    )
                    await self._set_status(
                        entry, AccountStatus.AUTH_REQUIRED, entry.health.last_error
                    )
                    return

                logger.warning(
                    "account_connection_failed",
                    account_id=str(entry.account_id),
                    error_type=type(exc).__name__,
                    retry_in=round(delay, 1),
                )
                await self._set_status(entry, AccountStatus.ERROR, entry.health.last_error)

            if entry.stop.is_set():
                break

            # Джиттер, чтобы десятки аккаунтов не ломились переподключаться разом.
            sleep_for = min(delay, self._settings.telegram_reconnect_max_delay)
            sleep_for *= 0.75 + random.random() * 0.5
            try:
                await asyncio.wait_for(entry.stop.wait(), timeout=sleep_for)
                break
            except TimeoutError:
                delay = min(delay * 2, self._settings.telegram_reconnect_max_delay)

    async def _connect_once(self, entry: _Entry) -> None:
        entry.health.connect_attempts += 1
        await self._set_status(entry, AccountStatus.AUTHENTICATING, None)

        client = entry.client
        if client is None:
            client = await self._factory.create(entry.account_id, entry.credentials, entry.proxy)
            entry.client = client

        await client.connect()
        entry.health.connected = True

        if not await client.is_user_authorized():
            entry.health.authorized = False
            entry.ready.set()
            await self._set_status(entry, AccountStatus.AUTH_REQUIRED, "session is not authorised")
            raise _AuthorisationLostError(entry.account_id)

        entry.health.authorized = True
        entry.health.connected_at = utcnow()
        entry.health.last_error = None

        if self._handler_factory is not None:
            self._register_handlers(entry)
            # Догоняем апдейты, пропущенные за простой/переподключение. Обработчик
            # уже навешен, поэтому события из catch_up пройдут через конвейер, а
            # повторы отсечёт claim() по processed_messages. Сбой догона не должен
            # ронять подключение — это лучшее усилие, а не обязательный шаг.
            try:
                await client.catch_up()
            except Exception as exc:  # noqa: BLE001 — догон не критичнее подключения
                logger.warning(
                    "catch_up_failed", account_id=str(entry.account_id), detail=str(exc)[:200]
                )

        entry.ready.set()
        await self._set_status(entry, AccountStatus.ONLINE, None)
        logger.info("account_connected", account_id=str(entry.account_id))

    def _register_handlers(self, entry: _Entry) -> None:
        from telethon import events

        assert self._handler_factory is not None
        assert entry.client is not None
        # Только входящие: собственные сообщения не должны попадать в конвейер,
        # иначе аккаунт начнёт отвечать сам себе (ТЗ §9).
        entry.client.add_event_handler(
            self._handler_factory(entry.account_id), events.NewMessage(incoming=True)
        )

    async def _wait_until_disconnected(self, entry: _Entry) -> None:
        """Спит, пока клиент жив, и возвращается, когда связь окончательно потеряна."""
        client = entry.client
        assert client is not None

        while not entry.stop.is_set():
            try:
                await asyncio.wait_for(entry.stop.wait(), timeout=5.0)
                return
            except TimeoutError:
                pass

            if not client.is_connected():
                entry.health.connected = False
                logger.warning("account_disconnected", account_id=str(entry.account_id))
                await self._set_status(entry, AccountStatus.OFFLINE, "connection lost")
                return

    async def _set_status(self, entry: _Entry, status: AccountStatus, error: str | None) -> None:
        if entry.health.status is status and error == entry.health.last_error:
            return
        entry.health.status = status
        if error is not None:
            entry.health.last_error = error
        if self._on_status is None:
            return
        try:
            await self._on_status(entry.account_id, status, error)
        except Exception as exc:  # noqa: BLE001 — уведомление не критично
            logger.warning("status_callback_failed", detail=str(exc))


class _AuthorisationLostError(RuntimeError):
    """Сессия есть, но Telegram её не принимает — нужен повторный вход."""

    def __init__(self, account_id: uuid.UUID) -> None:
        super().__init__(f"account {account_id} requires re-authentication")
