"""Исполнение команд, пришедших из API.

Каждая команда обрабатывается изолированно: ошибка одной не должна ронять
цикл чтения стрима и мешать остальным аккаунтам. Наружу уходит структурный
результат, а не исключение, — API превратит его в понятный HTTP-ответ.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.bus.messages import Command, CommandResult, CommandType
from app.core.config import Settings
from app.core.errors import AppError, InvalidInputError, TelegramError
from app.core.logging import get_logger, mask_phone
from app.database.repositories.accounts import AccountRepository
from app.database.session import Database
from app.telegram.account_manager import AccountManager
from app.telegram.auth_flow import AuthFlow, SignInResult
from app.telegram.client_manager import ClientManager
from app.telegram.sender import MessageSender
from app.telegram.session_manager import SessionManager

logger = get_logger(__name__)


class CommandHandler:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        accounts: AccountManager,
        clients: ClientManager,
        auth: AuthFlow,
        sessions: SessionManager,
        sender: MessageSender,
    ) -> None:
        self._settings = settings
        self._database = database
        self._accounts = accounts
        self._clients = clients
        self._auth = auth
        self._sessions = sessions
        self._sender = sender

    async def handle(self, command: Command) -> CommandResult:
        logger.info("command_received", **command.redacted())
        try:
            data = await self._dispatch(command)
        except AppError as exc:
            logger.warning("command_failed", code=exc.code, **command.redacted())
            return CommandResult.failure(command.id, exc.code, exc.message)
        except Exception as exc:
            # Цикл чтения стрима не должен падать из-за одной команды.
            logger.exception("command_crashed", **command.redacted())
            return CommandResult.failure(
                command.id, "internal_error", f"{type(exc).__name__}: {exc}"
            )
        return CommandResult.success(command.id, **data)

    async def _dispatch(self, command: Command) -> dict[str, Any]:
        handlers = {
            CommandType.PING: self._ping,
            CommandType.CONNECT: self._connect,
            CommandType.DISCONNECT: self._disconnect,
            CommandType.SEND_CODE: self._send_code,
            CommandType.SIGN_IN: self._sign_in,
            CommandType.SIGN_IN_PASSWORD: self._sign_in_password,
            CommandType.LOG_OUT: self._log_out,
            CommandType.LIST_DIALOGS: self._list_dialogs,
            CommandType.RESOLVE_CHAT: self._resolve_chat,
            CommandType.SEND_MESSAGE: self._send_message,
            CommandType.ACCOUNT_INFO: self._account_info,
            CommandType.CHAT_PHOTO: self._chat_photo,
        }
        handler = handlers.get(command.type)
        if handler is None:
            raise InvalidInputError(f"Unsupported command: {command.type}")
        return await handler(command)

    # --- служебные ---------------------------------------------------------
    async def _ping(self, command: Command) -> dict[str, Any]:
        return {"pong": True, "leased": command.account_id in self._accounts.leased_accounts}

    async def _account_info(self, command: Command) -> dict[str, Any]:
        health = self._clients.health(command.account_id)
        if health is None:
            return {"attached": False}
        return {
            "attached": True,
            "status": health.status.value,
            "connected": health.connected,
            "authorized": health.authorized,
            "last_error": health.last_error,
            "connect_attempts": health.connect_attempts,
        }

    # --- подключение -------------------------------------------------------
    async def _connect(self, command: Command) -> dict[str, Any]:
        async with self._database.session() as db:
            account = await AccountRepository(db).get(command.account_id)
        if account is None:
            raise InvalidInputError("Account not found", account_id=str(command.account_id))

        await self._accounts.poll()
        ready = await self._clients.wait_ready(
            command.account_id, timeout_seconds=self._settings.telegram_connect_timeout
        )
        return {"connected": ready}

    async def _disconnect(self, command: Command) -> dict[str, Any]:
        await self._accounts.detach(command.account_id)
        return {"disconnected": True}

    async def _log_out(self, command: Command) -> dict[str, Any]:
        await self._accounts.detach(command.account_id, logout=True)
        return {"logged_out": True}

    # --- авторизация -------------------------------------------------------
    async def _send_code(self, command: Command) -> dict[str, Any]:
        phone = self._required(command, "phone")
        api_id, api_hash = self._api_credentials(command)

        await self._auth.send_code(
            command.account_id, phone=phone, api_id=api_id, api_hash=api_hash
        )
        return {"code_sent": True, "phone": mask_phone(phone)}

    async def _sign_in(self, command: Command) -> dict[str, Any]:
        code = self._required(command, "code")
        result = await self._auth.sign_in(command.account_id, code)
        if result is None:
            return {"authorized": False, "password_required": True}
        await self._persist_sign_in(command.account_id, result)
        return {"authorized": True, "password_required": False, "tg_user_id": result.tg_user_id}

    async def _sign_in_password(self, command: Command) -> dict[str, Any]:
        password = self._required(command, "password")
        result = await self._auth.sign_in_password(command.account_id, password)
        await self._persist_sign_in(command.account_id, result)
        return {"authorized": True, "tg_user_id": result.tg_user_id}

    async def _persist_sign_in(self, account_id: uuid.UUID, result: SignInResult) -> None:
        async with self._database.session() as db:
            await self._sessions.store(
                db,
                account_id,
                session_string=result.credentials.session_string,
                api_id=result.credentials.api_id,
                api_hash=result.credentials.api_hash,
            )
            await AccountRepository(db).update_identity(
                account_id,
                tg_user_id=result.tg_user_id,
                username=result.username,
                display_name=result.display_name,
            )
        await self._accounts.adopt(account_id, result.credentials)

    # --- работа с чатами ---------------------------------------------------
    async def _list_dialogs(self, command: Command) -> dict[str, Any]:
        from telethon.errors import TypeNotFoundError

        client = self._require_client(command.account_id)
        limit = int(command.payload.get("limit", 100))
        try:
            dialogs = await client.get_dialogs(limit=limit)
        except TypeNotFoundError as exc:
            # Telegram выкатил новый тип чата, которого нет в схеме Telethon.
            # Разобрать список целиком нельзя, но приём сообщений это не
            # ломает — чат можно добавить вручную по @имени или ID.
            logger.warning("dialogs_unparsable", constructor=hex(exc.invalid_constructor_id))
            raise TelegramError(
                "Telegram прислал новый тип чата, который не понимает текущая "
                "версия Telethon. Добавьте чат вручную по @имени или ID — "
                "на приём сообщений это не влияет."
            ) from None

        items = []
        for dialog in dialogs:
            entity = getattr(dialog, "entity", None)
            items.append(
                {
                    "tg_chat_id": int(getattr(dialog, "id", 0)),
                    "title": getattr(dialog, "name", None),
                    "username": getattr(entity, "username", None),
                    "is_group": bool(getattr(dialog, "is_group", False)),
                    "is_channel": bool(getattr(dialog, "is_channel", False)),
                    "is_user": bool(getattr(dialog, "is_user", False)),
                }
            )
        return {"dialogs": items}

    async def _resolve_chat(self, command: Command) -> dict[str, Any]:
        client = self._require_client(command.account_id)
        reference: str | int = self._required(command, "reference")
        if isinstance(reference, str) and reference.lstrip("-").isdigit():
            reference = int(reference)

        entity = await client.get_entity(reference)
        return {
            "tg_chat_id": int(getattr(entity, "id", 0)),
            "title": getattr(entity, "title", None) or _user_title(entity),
            "username": getattr(entity, "username", None),
        }

    async def _send_message(self, command: Command) -> dict[str, Any]:
        client = self._require_client(command.account_id)
        chat_id = int(self._required(command, "chat_id"))
        text = str(self._required(command, "text"))
        reply_to = command.payload.get("reply_to")

        sent = await self._sender.send(
            command.account_id,
            client,
            chat_id=chat_id,
            text=text,
            reply_to=int(reply_to) if reply_to is not None else None,
        )
        return {"tg_message_id": sent.tg_message_id, "chat_id": sent.chat_id}

    async def _chat_photo(self, command: Command) -> dict[str, Any]:
        """Скачивает аватар чата в память. Пусто — фото нет, это не ошибка."""
        import base64
        import io

        client = self._require_client(command.account_id)
        chat_id = int(self._required(command, "chat_id"))

        try:
            entity = await client.get_entity(chat_id)
            buffer = io.BytesIO()
            # download_profile_photo без указания файла вернёт None, если фото
            # нет; иначе запишет JPEG в переданный буфер.
            result = await client.download_profile_photo(entity, file=buffer)
        except Exception as exc:  # noqa: BLE001 — фото не критично
            return {"has_photo": False, "detail": str(exc)[:120]}

        if result is None:
            return {"has_photo": False}
        return {
            "has_photo": True,
            "mime": "image/jpeg",
            "data": base64.b64encode(buffer.getvalue()).decode("ascii"),
        }

    # --- вспомогательное ---------------------------------------------------
    def _require_client(self, account_id: uuid.UUID) -> Any:
        client = self._clients.get(account_id)
        if client is None:
            raise InvalidInputError(
                "Account is not connected on this worker", account_id=str(account_id)
            )
        return client

    @staticmethod
    def _required(command: Command, field: str) -> Any:
        value = command.payload.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise InvalidInputError(f"Field '{field}' is required for {command.type}")
        return value

    def _api_credentials(self, command: Command) -> tuple[int, str]:
        """api_id/api_hash аккаунта или общие из окружения.

        Дефолтных «публичных» значений нет намеренно: вход со чужим api_id —
        нарушение условий Telegram и частая причина блокировки аккаунта.
        """
        raw_id = command.payload.get("api_id") or self._settings.telegram_api_id
        raw_hash = command.payload.get("api_hash") or (
            self._settings.telegram_api_hash.get_secret_value()
            if self._settings.telegram_api_hash
            else None
        )
        if not raw_id or not raw_hash:
            raise InvalidInputError(
                "Нужны собственные api_id и api_hash с my.telegram.org — "
                "задайте их в настройках аккаунта или в TELEGRAM_API_ID/TELEGRAM_API_HASH"
            )
        return int(raw_id), str(raw_hash)


def _user_title(entity: Any) -> str | None:
    parts = [getattr(entity, "first_name", None), getattr(entity, "last_name", None)]
    name = " ".join(part for part in parts if part)
    return name or None
