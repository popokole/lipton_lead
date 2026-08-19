"""Жизненный цикл аккаунтов внутри воркера (ТЗ §3).

Связывает четыре вещи: аренду в Redis, зашифрованную сессию в PostgreSQL,
живой клиент Telethon и статус, который видит панель.

Порядок операций важен. Аренда берётся ДО подключения: если сначала
подключиться, а потом обнаружить, что аккаунт уже обслуживает другой воркер,
Telegram успеет разлогинить сессию. Аренда отпускается ПОСЛЕ отключения — по
той же причине в обратную сторону.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any

from app.bus.events import EventPublisher
from app.bus.messages import Event, EventType
from app.core.clock import utcnow
from app.core.config import Settings
from app.core.crypto import DecryptionError
from app.core.logging import get_logger
from app.database.repositories.accounts import AccountRepository
from app.database.session import Database
from app.models import Account, AccountStatus
from app.telegram.client import proxy_to_telethon
from app.telegram.client_manager import ClientManager
from app.telegram.session_manager import SessionCredentials, SessionManager
from app.workers.lease import AccountLease

logger = get_logger(__name__)


class AccountManager:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        client_manager: ClientManager,
        session_manager: SessionManager,
        lease: AccountLease,
        publisher: EventPublisher,
    ) -> None:
        self._settings = settings
        self._database = database
        self._clients = client_manager
        self._sessions = session_manager
        self._lease = lease
        self._publisher = publisher
        self._leased: set[uuid.UUID] = set()

    @property
    def leased_accounts(self) -> set[uuid.UUID]:
        return set(self._leased)

    # --- подбор аккаунтов --------------------------------------------------
    async def poll(self) -> None:
        """Берёт в работу свободные аккаунты, до лимита воркера."""
        await self._start_pending()

        free_slots = self._settings.worker_max_accounts - len(self._leased)
        if free_slots <= 0:
            return

        async with self._database.session() as db:
            repo = AccountRepository(db)
            candidates = await repo.list_serviceable(self._settings.worker_max_accounts * 2)

        for account in candidates:
            if free_slots <= 0:
                break
            if account.id in self._leased:
                continue
            if await self._attach(account):
                free_slots -= 1

    async def _attach(self, account: Account) -> bool:
        if not await self._lease.try_acquire(account.id):
            return False

        self._leased.add(account.id)
        try:
            credentials, proxy = await self._load_credentials(account.id, account.proxy_id)
        except DecryptionError as exc:
            # Ключ шифрования не подходит: чинить нечего, аккаунт нужно
            # авторизовать заново — но аренду держать смысла нет.
            logger.error("session_decryption_failed", account_id=str(account.id))
            await self._persist_status(
                account.id, AccountStatus.AUTH_REQUIRED, f"session is unreadable: {exc}"
            )
            await self._release(account.id)
            return False
        except ValueError as exc:
            logger.error("session_unusable", account_id=str(account.id), detail=str(exc))
            await self._persist_status(account.id, AccountStatus.AUTH_REQUIRED, str(exc))
            await self._release(account.id)
            return False

        if credentials is None:
            # Сессии ещё нет — аккаунт ждёт входа через панель. Аренду при этом
            # держим: именно по ней API находит воркер, которому адресовать
            # команду «отправить код». Клиент не поднимаем — поднимать нечего.
            await self._mark_leased(account.id)
            await self._persist_status(
                account.id, AccountStatus.AUTH_REQUIRED, "требуется авторизация"
            )
            return True

        await self._mark_leased(account.id)
        await self._clients.start(account.id, credentials, proxy)
        return True

    async def _start_pending(self) -> None:
        """Поднимает клиентов у аккаунтов, арендованных до появления сессии.

        Такой аккаунт мы держим ради команд входа, но клиента у него нет.
        Сессия может появиться позже и без нашего участия — например, после
        импорта tdata через панель. Если не перепроверять, аккаунт останется
        в аренде навсегда и никогда не подключится.
        """
        pending = [
            account_id for account_id in self._leased if self._clients.health(account_id) is None
        ]
        if not pending:
            return

        async with self._database.session() as db:
            repository = AccountRepository(db)
            accounts = [await repository.get(account_id) for account_id in pending]

        for account in accounts:
            if account is None or not account.enabled:
                continue
            try:
                credentials, proxy = await self._load_credentials(account.id, account.proxy_id)
            except (DecryptionError, ValueError) as exc:
                logger.error(
                    "pending_session_unusable", account_id=str(account.id), detail=str(exc)
                )
                await self._persist_status(account.id, AccountStatus.AUTH_REQUIRED, str(exc))
                continue

            if credentials is None:
                continue  # всё ещё ждёт авторизации — это норма

            if await self._clients.start(account.id, credentials, proxy):
                logger.info("pending_account_activated", account_id=str(account.id))

    async def _load_credentials(
        self, account_id: uuid.UUID, proxy_id: uuid.UUID | None
    ) -> tuple[SessionCredentials | None, dict[str, Any] | None]:
        async with self._database.session() as db:
            credentials = await self._sessions.load(db, account_id)
            proxy_config: dict[str, Any] | None = None
            if proxy_id is not None:
                proxy = await AccountRepository(db).get_proxy(proxy_id)
                if proxy is not None and proxy.enabled:
                    password = None
                    if proxy.password_ct and proxy.password_nonce and proxy.password_key_id:
                        password = self._sessions.decrypt_secret(
                            proxy.password_ct, proxy.password_nonce, proxy.password_key_id
                        )
                    proxy_config = proxy_to_telethon(
                        proxy.scheme, proxy.host, proxy.port, proxy.username, password
                    )
        return credentials, proxy_config

    # --- аренда ------------------------------------------------------------
    async def renew_leases(self) -> None:
        """Продлевает аренды и отпускает аккаунты, чью аренду мы потеряли."""
        lost: list[uuid.UUID] = []
        for account_id in list(self._leased):
            if not await self._lease.renew(account_id):
                lost.append(account_id)

        for account_id in lost:
            logger.warning("account_lease_lost", account_id=str(account_id))
            self._leased.discard(account_id)
            await self._clients.stop(account_id)
            await self._persist_status(account_id, AccountStatus.OFFLINE, "lease lost")

    async def _mark_leased(self, account_id: uuid.UUID) -> None:
        expires = utcnow() + timedelta(seconds=self._settings.account_lease_ttl_seconds)
        async with self._database.session() as db:
            await AccountRepository(db).attach_worker(
                account_id, uuid.UUID(self._lease.worker_id), expires
            )

    async def _release(self, account_id: uuid.UUID) -> None:
        self._leased.discard(account_id)
        await self._lease.release(account_id)
        async with self._database.session() as db:
            await AccountRepository(db).detach_worker(account_id)

    # --- команды панели ----------------------------------------------------
    async def detach(self, account_id: uuid.UUID, *, logout: bool = False) -> None:
        await self._clients.stop(account_id, logout=logout)
        if logout:
            async with self._database.session() as db:
                await self._sessions.revoke(db, account_id)
        await self._release(account_id)
        await self._persist_status(account_id, AccountStatus.OFFLINE, None)

    async def adopt(self, account_id: uuid.UUID, credentials: SessionCredentials) -> None:
        """Берёт под управление только что авторизованный аккаунт."""
        if account_id not in self._leased:
            if not await self._lease.try_acquire(account_id):
                logger.warning("adopt_without_lease", account_id=str(account_id))
                return
            self._leased.add(account_id)
        await self._mark_leased(account_id)
        await self._clients.start(account_id, credentials)

    # --- статусы -----------------------------------------------------------
    async def on_client_status(
        self, account_id: uuid.UUID, status: AccountStatus, error: str | None
    ) -> None:
        await self._persist_status(account_id, status, error)

    async def _persist_status(
        self, account_id: uuid.UUID, status: AccountStatus, error: str | None
    ) -> None:
        try:
            async with self._database.session() as db:
                await AccountRepository(db).set_status(account_id, status, error)
        except Exception as exc:  # noqa: BLE001 — статус не важнее работы аккаунта
            logger.warning(
                "account_status_persist_failed", account_id=str(account_id), detail=str(exc)
            )

        await self._publisher.publish(
            Event(
                type=EventType.ACCOUNT_STATUS,
                account_id=account_id,
                payload={"status": status.value, "error": error},
            )
        )

    async def shutdown(self) -> None:
        await self._clients.shutdown()
        await asyncio.gather(
            *(self._lease.release(account_id) for account_id in list(self._leased)),
            return_exceptions=True,
        )
        self._leased.clear()
