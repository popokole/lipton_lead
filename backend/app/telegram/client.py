"""Абстракция над TelegramClient и её настоящая реализация.

Протокол здесь не ради красоты: без него ни одну ветку управления клиентом
нельзя проверить тестом, не подключаясь к Telegram по-настоящему. ТЗ §37 это
прямо запрещает, поэтому реальный клиент создаётся фабрикой, а в тестах
подставляется фальшивая.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from app.core.config import Settings
from app.core.logging import get_logger
from app.telegram.session_manager import SessionCredentials

logger = get_logger(__name__)

EventHandler = Callable[[Any], Awaitable[None]]


class TelegramClientLike(Protocol):
    """Та часть Telethon, которой пользуется платформа."""

    async def connect(self) -> None: ...

    async def catch_up(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def is_user_authorized(self) -> bool: ...

    async def get_me(self) -> Any: ...

    async def log_out(self) -> bool: ...

    def add_event_handler(self, callback: EventHandler, event: Any = None) -> None: ...

    def is_connected(self) -> bool: ...

    async def send_message(
        self, entity: Any, message: str, *, reply_to: int | None = None
    ) -> Any: ...

    async def send_read_acknowledge(self, entity: Any, *, max_id: int | None = None) -> Any: ...

    def action(self, entity: Any, action: str) -> Any:
        """Индикатор «печатает» — используется как `async with client.action(...)`."""
        ...

    async def get_dialogs(self, limit: int | None = None) -> Any: ...

    async def get_entity(self, entity: Any) -> Any: ...

    async def download_profile_photo(self, entity: Any, *, file: Any = None) -> Any: ...

    async def send_code_request(self, phone: str) -> Any: ...

    async def sign_in(
        self,
        phone: str | None = None,
        code: str | None = None,
        *,
        password: str | None = None,
        phone_code_hash: str | None = None,
    ) -> Any: ...


class ClientFactory(Protocol):
    async def create(
        self, account_id: uuid.UUID, credentials: SessionCredentials, proxy: dict[str, Any] | None
    ) -> TelegramClientLike: ...


class TelethonClientFactory:
    """Создаёт настоящий TelegramClient.

    Каждый аккаунт получает свой экземпляр: общий клиент означал бы общую
    сессию, а значит разлогин при первом же параллельном подключении.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def create(
        self, account_id: uuid.UUID, credentials: SessionCredentials, proxy: dict[str, Any] | None
    ) -> TelegramClientLike:
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        client = TelegramClient(
            StringSession(credentials.session_string),
            credentials.api_id,
            credentials.api_hash,
            device_model=self._settings.telegram_device_model,
            system_version=self._settings.telegram_system_version,
            app_version=self._settings.telegram_app_version,
            timeout=self._settings.telegram_connect_timeout,
            # Telethon сам переподключается; наш супервизор нужен для случаев,
            # когда переподключение уже не поможет — например, слетела сессия.
            connection_retries=None,
            retry_delay=self._settings.telegram_reconnect_base_delay,
            auto_reconnect=True,
            # Догоняем апдейты, пропущенные за время простоя/переподключения:
            # без этого сообщения из окна рестарта теряются навсегда. Повторную
            # обработку отсекает claim() по processed_messages.
            catch_up=True,
            proxy=proxy,
        )
        logger.debug("telegram_client_created", account_id=str(account_id))
        return client


def proxy_to_telethon(
    scheme: str, host: str, port: int, username: str | None, password: str | None
) -> dict[str, Any]:
    """Настройки прокси в формате python-socks, который понимает Telethon."""
    proxy: dict[str, Any] = {"proxy_type": scheme, "addr": host, "port": port}
    if username:
        proxy["username"] = username
    if password:
        proxy["password"] = password
    return proxy
