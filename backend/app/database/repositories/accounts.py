"""Запросы по аккаунтам.

Репозиторий отвечает только за доступ к данным: решения о том, брать ли
аккаунт в работу, принимает воркер.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import or_ as sa_or
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utcnow
from app.models import Account, AccountStatus, Proxy, TelegramSession


class AccountRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get(self, account_id: uuid.UUID) -> Account | None:
        return await self._db.get(Account, account_id)

    async def list_serviceable(self, limit: int) -> list[Account]:
        """Аккаунты, которые воркер может взять под свою ответственность.

        Сюда попадают и аккаунты без сессии. Это не ошибка: команда «отправить
        код входа» адресуется владельцу аренды, а до первой авторизации сессии
        ещё нет. Если бы воркер брал только аккаунты с сессией, войти в новый
        аккаунт было бы невозможно — команду просто некому доставить.

        Клиент для такого аккаунта не поднимается, он лишь закрепляется за
        воркером до завершения входа.
        """
        stmt = (
            select(Account)
            .outerjoin(TelegramSession, TelegramSession.account_id == Account.id)
            .where(
                Account.enabled.is_(True),
                Account.status != AccountStatus.DISABLED,
                sa_or(
                    TelegramSession.id.is_(None),
                    TelegramSession.revoked_at.is_(None),
                ),
            )
            .order_by(Account.created_at)
            .limit(limit)
        )
        return list((await self._db.scalars(stmt)).all())

    async def get_proxy(self, proxy_id: uuid.UUID) -> Proxy | None:
        return await self._db.get(Proxy, proxy_id)

    async def set_status(
        self, account_id: uuid.UUID, status: AccountStatus, error: str | None = None
    ) -> None:
        values: dict[str, object] = {"status": status, "updated_at": utcnow()}
        if error is not None:
            values["last_error"] = error
            values["last_error_at"] = utcnow()
        elif status is AccountStatus.ONLINE:
            # Успешное подключение снимает прошлую ошибку: иначе панель вечно
            # показывает давно исправленную проблему.
            values["last_error"] = None
            values["last_seen_at"] = utcnow()

        await self._db.execute(update(Account).where(Account.id == account_id).values(**values))

    async def attach_worker(
        self, account_id: uuid.UUID, worker_id: uuid.UUID, lease_expires_at: datetime
    ) -> None:
        await self._db.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(worker_id=worker_id, lease_expires_at=lease_expires_at)
        )

    async def detach_worker(self, account_id: uuid.UUID) -> None:
        await self._db.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(worker_id=None, lease_expires_at=None)
        )

    async def detach_all_of_worker(self, worker_id: uuid.UUID) -> None:
        await self._db.execute(
            update(Account)
            .where(Account.worker_id == worker_id)
            .values(worker_id=None, lease_expires_at=None)
        )

    async def update_identity(
        self,
        account_id: uuid.UUID,
        *,
        tg_user_id: int,
        username: str | None,
        display_name: str | None,
    ) -> None:
        await self._db.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(
                tg_user_id=tg_user_id,
                username=username,
                display_name=display_name,
                updated_at=utcnow(),
            )
        )

    async def own_telegram_ids(self) -> set[int]:
        """Идентификаторы всех подключённых аккаунтов.

        Нужны конвейеру: сообщение от любого из них — наше собственное, и
        отвечать на него нельзя, иначе два аккаунта зациклятся друг на друге.
        """
        rows = await self._db.scalars(
            select(Account.tg_user_id).where(Account.tg_user_id.is_not(None))
        )
        return {row for row in rows if row is not None}
