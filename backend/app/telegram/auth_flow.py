"""Авторизация Telegram-аккаунта (вход по номеру телефона).

Весь процесс выполняется внутри воркера от начала до конца. Причина
техническая, а не стилистическая: `phone_code_hash`, который Telegram выдаёт
на запрос кода, действителен только для того соединения, которое его
запросило. Отправить код из API, а войти из воркера физически нельзя.

Промежуточное состояние (телефон, phone_code_hash, незавершённый клиент)
живёт только в памяти процесса и исчезает при остановке. В базу и в логи
ничего из этого не попадает: код входа и пароль 2FA — самые чувствительные
данные во всей системе.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

from app.core.clock import utcnow
from app.core.config import Settings
from app.core.errors import InvalidInputError, TelegramError
from app.core.logging import get_logger, mask_phone
from app.telegram.client import ClientFactory, TelegramClientLike
from app.telegram.session_manager import SessionCredentials

logger = get_logger(__name__)

# Код из Telegram живёт считаные минуты; держать незавершённый вход дольше
# незачем — он занимает соединение.
PENDING_TTL_SECONDS = 600


@dataclass
class _Pending:
    account_id: uuid.UUID
    phone: str
    phone_code_hash: str
    client: TelegramClientLike
    api_id: int
    api_hash: str
    started_at: float

    def __repr__(self) -> str:  # phone_code_hash не должен всплыть в трейсбеке
        return f"_Pending(account_id={self.account_id}, phone={mask_phone(self.phone)})"


@dataclass(frozen=True, slots=True)
class SignInResult:
    credentials: SessionCredentials
    tg_user_id: int
    username: str | None
    display_name: str | None


class AuthFlow:
    """Пошаговый вход: код → (2FA) → готовая сессия."""

    def __init__(self, settings: Settings, factory: ClientFactory) -> None:
        self._settings = settings
        self._factory = factory
        self._pending: dict[uuid.UUID, _Pending] = {}
        self._lock = asyncio.Lock()

    # --- шаг 1: запрос кода ------------------------------------------------
    async def send_code(
        self,
        account_id: uuid.UUID,
        *,
        phone: str,
        api_id: int,
        api_hash: str,
        proxy: dict[str, Any] | None = None,
    ) -> None:
        from telethon.errors import RPCError

        async with self._lock:
            await self._discard(account_id)
            self._evict_expired()

            # Пустая строковая сессия: новый вход начинается с чистого листа.
            client = await self._factory.create(
                account_id, SessionCredentials("", api_id, api_hash), proxy
            )
            try:
                await client.connect()
                sent = await client.send_code_request(phone)
            except RPCError as exc:
                await self._safe_disconnect(client)
                raise TelegramError(f"{type(exc).__name__}: {exc}") from exc
            except Exception:
                await self._safe_disconnect(client)
                raise

            self._pending[account_id] = _Pending(
                account_id=account_id,
                phone=phone,
                phone_code_hash=sent.phone_code_hash,
                client=client,
                api_id=api_id,
                api_hash=api_hash,
                started_at=asyncio.get_running_loop().time(),
            )

        logger.info("login_code_requested", account_id=str(account_id), phone=mask_phone(phone))

    # --- шаг 2: код из Telegram -------------------------------------------
    async def sign_in(self, account_id: uuid.UUID, code: str) -> SignInResult | None:
        """Возвращает результат входа либо None, если требуется пароль 2FA."""
        from telethon.errors import (
            PhoneCodeExpiredError,
            PhoneCodeInvalidError,
            RPCError,
            SessionPasswordNeededError,
        )

        pending = self._require_pending(account_id)
        try:
            await pending.client.sign_in(
                phone=pending.phone, code=code, phone_code_hash=pending.phone_code_hash
            )
        except SessionPasswordNeededError:
            logger.info("login_needs_password", account_id=str(account_id))
            return None
        except PhoneCodeInvalidError as exc:
            raise InvalidInputError("Неверный код подтверждения") from exc
        except PhoneCodeExpiredError as exc:
            await self._discard(account_id)
            raise InvalidInputError("Код истёк, запросите новый") from exc
        except RPCError as exc:
            raise TelegramError(f"{type(exc).__name__}: {exc}") from exc

        return await self._finish(account_id)

    # --- шаг 3: пароль двухфакторной защиты -------------------------------
    async def sign_in_password(self, account_id: uuid.UUID, password: str) -> SignInResult:
        from telethon.errors import PasswordHashInvalidError, RPCError

        pending = self._require_pending(account_id)
        try:
            await pending.client.sign_in(password=password)
        except PasswordHashInvalidError as exc:
            raise InvalidInputError("Неверный пароль двухфакторной защиты") from exc
        except RPCError as exc:
            raise TelegramError(f"{type(exc).__name__}: {exc}") from exc

        result = await self._finish(account_id)
        if result is None:
            raise TelegramError("Telegram did not complete the sign-in")
        return result

    async def cancel(self, account_id: uuid.UUID) -> None:
        async with self._lock:
            await self._discard(account_id)

    def is_pending(self, account_id: uuid.UUID) -> bool:
        return account_id in self._pending

    async def shutdown(self) -> None:
        async with self._lock:
            for account_id in list(self._pending):
                await self._discard(account_id)

    # --- внутреннее --------------------------------------------------------
    def _require_pending(self, account_id: uuid.UUID) -> _Pending:
        pending = self._pending.get(account_id)
        if pending is None:
            raise InvalidInputError(
                "Нет активной попытки входа — запросите код заново",
                account_id=str(account_id),
            )
        return pending

    async def _finish(self, account_id: uuid.UUID) -> SignInResult | None:
        from telethon.sessions import StringSession

        pending = self._require_pending(account_id)
        client = pending.client

        me = await client.get_me()
        if me is None:
            raise TelegramError("Telegram did not return the signed-in user")

        session = getattr(client, "session", None)
        if not isinstance(session, StringSession):
            raise TelegramError("client is not using a string session")

        credentials = SessionCredentials(
            session_string=session.save(), api_id=pending.api_id, api_hash=pending.api_hash
        )
        result = SignInResult(
            credentials=credentials,
            tg_user_id=int(me.id),
            username=getattr(me, "username", None),
            display_name=_display_name(me),
        )

        async with self._lock:
            # Клиент авторизован, но дальше аккаунтом управляет ClientManager:
            # два живых подключения к одной сессии недопустимы.
            await self._discard(account_id)

        logger.info(
            "login_completed",
            account_id=str(account_id),
            tg_user_id=result.tg_user_id,
            completed_at=utcnow().isoformat(),
        )
        return result

    async def _discard(self, account_id: uuid.UUID) -> None:
        pending = self._pending.pop(account_id, None)
        if pending is not None:
            await self._safe_disconnect(pending.client)

    def _evict_expired(self) -> None:
        now = asyncio.get_running_loop().time()
        stale = [
            account_id
            for account_id, pending in self._pending.items()
            if now - pending.started_at > PENDING_TTL_SECONDS
        ]
        for account_id in stale:
            pending = self._pending.pop(account_id)
            logger.info("login_attempt_expired", account_id=str(account_id))
            asyncio.ensure_future(self._safe_disconnect(pending.client))  # noqa: RUF006

    @staticmethod
    async def _safe_disconnect(client: TelegramClientLike) -> None:
        try:
            await client.disconnect()
        except Exception as exc:  # noqa: BLE001 — освобождение ресурсов не критично
            logger.warning("pending_client_disconnect_failed", detail=str(exc))


def _display_name(user: Any) -> str | None:
    parts = [getattr(user, "first_name", None), getattr(user, "last_name", None)]
    name = " ".join(part for part in parts if part)
    return name or None
