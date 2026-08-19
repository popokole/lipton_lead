"""Telegram-аккаунты (ТЗ §3, §26 /api/accounts).

Всё, что требует живого подключения — вход по коду, список диалогов, — API
выполняет не сам, а просит воркер, который держит аренду аккаунта. Свой
Telethon-клиент API не поднимает никогда: второе подключение к той же сессии
приводит к разлогину аккаунта в Telegram.

Код из SMS и пароль 2FA проходят через API транзитом: они не сохраняются ни в
базе, ни в логах — редактор логов вырезает эти поля по имени.
"""

from __future__ import annotations

import contextlib
import uuid

from fastapi import APIRouter, Query
from sqlalchemy import delete, select

from app.api.deps import (
    AdminUser,
    CommandBusDep,
    CurrentUser,
    DbDep,
    OperatorUser,
    RuntimeDep,
)
from app.bus.messages import Command, CommandType
from app.core.errors import AppError, ConflictError, NotFoundError
from app.database.repositories.accounts import AccountRepository
from app.models import Account, AccountStatus
from app.schemas.common import Ok
from app.schemas.resources import (
    AccountCreate,
    AccountOut,
    AccountUpdate,
    AuthStepResult,
    DialogOut,
    SendCodeRequest,
    SignInPasswordRequest,
    SignInRequest,
)

router = APIRouter(prefix="/accounts", tags=["accounts"])


async def _load(db: DbDep, account_id: uuid.UUID) -> Account:
    account = await AccountRepository(db).get(account_id)
    if account is None:
        raise NotFoundError("Аккаунт не найден", account_id=str(account_id))
    return account


@router.get("", response_model=list[AccountOut], summary="Список аккаунтов")
async def list_accounts(
    _user: CurrentUser, db: DbDep, enabled: bool | None = Query(default=None)
) -> list[AccountOut]:
    stmt = select(Account).order_by(Account.created_at)
    if enabled is not None:
        stmt = stmt.where(Account.enabled.is_(enabled))
    rows = await db.scalars(stmt)
    return [AccountOut.model_validate(row) for row in rows.all()]


@router.post("", response_model=AccountOut, status_code=201, summary="Добавить аккаунт")
async def create_account(payload: AccountCreate, user: OperatorUser, db: DbDep) -> AccountOut:
    account = Account(label=payload.label, owner_user_id=user.id, status=AccountStatus.CREATED)
    db.add(account)
    await db.flush()
    return AccountOut.model_validate(account)


@router.get("/{account_id}", response_model=AccountOut, summary="Карточка аккаунта")
async def get_account(account_id: uuid.UUID, _user: CurrentUser, db: DbDep) -> AccountOut:
    return AccountOut.model_validate(await _load(db, account_id))


@router.patch("/{account_id}", response_model=AccountOut, summary="Изменить аккаунт")
async def update_account(
    account_id: uuid.UUID,
    payload: AccountUpdate,
    _user: OperatorUser,
    bus: CommandBusDep,
    db: DbDep,
) -> AccountOut:
    account = await _load(db, account_id)
    if payload.label is not None:
        account.label = payload.label
    if payload.enabled is not None:
        account.enabled = payload.enabled
        if not payload.enabled:
            # Воркер перестанет брать аккаунт в работу на следующем опросе.
            account.status = AccountStatus.DISABLED
        elif account.status is AccountStatus.DISABLED:
            account.status = AccountStatus.OFFLINE

    proxy_changed = False
    if payload.detach_proxy:
        proxy_changed = account.proxy_id is not None
        account.proxy_id = None
    elif payload.proxy_id is not None and payload.proxy_id != account.proxy_id:
        account.proxy_id = payload.proxy_id
        proxy_changed = True

    await db.flush()

    if proxy_changed:
        # Фиксируем ДО обращения к воркеру. Иначе он перечитает аккаунт из базы
        # раньше коммита, увидит старые настройки сети и поднимет клиента с
        # ними — ровно то, от чего мы его и просим отключиться.
        await db.commit()

        # Клиент уже подключён со старыми настройками. Отсоединяем его: воркер
        # поднимет аккаунт заново на ближайшем опросе, уже через прокси.
        with contextlib.suppress(AppError):
            await bus.call(
                Command(type=CommandType.DISCONNECT, account_id=account_id), timeout_seconds=20
            )

    return AccountOut.model_validate(account)


@router.delete("/{account_id}", response_model=Ok, summary="Удалить аккаунт")
async def delete_account(account_id: uuid.UUID, _admin: AdminUser, db: DbDep) -> Ok:
    await _load(db, account_id)
    # Каскад в базе унесёт сессию, чаты, сообщения и действия аккаунта.
    await db.execute(delete(Account).where(Account.id == account_id))
    return Ok(detail="Аккаунт удалён")


# --- авторизация в Telegram -------------------------------------------------
@router.post(
    "/{account_id}/send-code", response_model=AuthStepResult, summary="Запросить код входа"
)
async def send_code(
    account_id: uuid.UUID,
    payload: SendCodeRequest,
    _user: OperatorUser,
    runtime: RuntimeDep,
    bus: CommandBusDep,
    db: DbDep,
) -> AuthStepResult:
    settings = runtime.settings
    if payload.api_id is None and settings.telegram_api_id is None:
        raise ConflictError(
            "Нужны собственные api_id и api_hash с my.telegram.org — "
            "передайте их здесь или задайте TELEGRAM_API_ID/TELEGRAM_API_HASH"
        )

    account = await _load(db, account_id)
    account.phone_e164 = payload.phone
    account.status = AccountStatus.AUTHENTICATING
    await db.flush()

    credentials: dict[str, object] = {"phone": payload.phone}
    if payload.api_id is not None:
        credentials["api_id"] = payload.api_id
    if payload.api_hash:
        credentials["api_hash"] = payload.api_hash

    result = await bus.call(
        Command(type=CommandType.SEND_CODE, account_id=account_id, payload=credentials),
        timeout_seconds=settings.telegram_connect_timeout + 15,
    )
    return AuthStepResult(ok=result.ok, detail=result.error_message)


@router.post("/{account_id}/sign-in", response_model=AuthStepResult, summary="Ввести код")
async def sign_in(
    account_id: uuid.UUID,
    payload: SignInRequest,
    _user: OperatorUser,
    bus: CommandBusDep,
    db: DbDep,
) -> AuthStepResult:
    await _load(db, account_id)
    result = await bus.call(
        Command(type=CommandType.SIGN_IN, account_id=account_id, payload={"code": payload.code}),
        timeout_seconds=60,
    )
    return AuthStepResult(
        ok=result.ok,
        password_required=bool(result.data.get("password_required")),
        authorized=bool(result.data.get("authorized")),
        detail=result.error_message,
    )


@router.post(
    "/{account_id}/sign-in-password",
    response_model=AuthStepResult,
    summary="Ввести пароль двухфакторной защиты",
)
async def sign_in_password(
    account_id: uuid.UUID,
    payload: SignInPasswordRequest,
    _user: OperatorUser,
    bus: CommandBusDep,
    db: DbDep,
) -> AuthStepResult:
    await _load(db, account_id)
    result = await bus.call(
        Command(
            type=CommandType.SIGN_IN_PASSWORD,
            account_id=account_id,
            payload={"password": payload.password},
        ),
        timeout_seconds=60,
    )
    return AuthStepResult(
        ok=result.ok,
        authorized=bool(result.data.get("authorized")),
        detail=result.error_message,
    )


@router.post("/{account_id}/logout", response_model=Ok, summary="Выйти из Telegram")
async def logout(account_id: uuid.UUID, _user: OperatorUser, bus: CommandBusDep, db: DbDep) -> Ok:
    await _load(db, account_id)
    result = await bus.call(
        Command(type=CommandType.LOG_OUT, account_id=account_id), timeout_seconds=60
    )
    return Ok(ok=result.ok, detail=result.error_message or "Аккаунт разлогинен")


@router.get("/{account_id}/dialogs", response_model=list[DialogOut], summary="Мои диалоги")
async def dialogs(
    account_id: uuid.UUID,
    _user: CurrentUser,
    bus: CommandBusDep,
    db: DbDep,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[DialogOut]:
    await _load(db, account_id)
    result = await bus.call(
        Command(type=CommandType.LIST_DIALOGS, account_id=account_id, payload={"limit": limit}),
        timeout_seconds=60,
    )
    if not result.ok:
        raise ConflictError(result.error_message or "Не удалось получить диалоги")
    return [DialogOut.model_validate(item) for item in result.data.get("dialogs", [])]


@router.get("/{account_id}/status", summary="Состояние подключения на воркере")
async def account_status(
    account_id: uuid.UUID, _user: CurrentUser, bus: CommandBusDep, db: DbDep
) -> dict[str, object]:
    await _load(db, account_id)
    result = await bus.call(
        Command(type=CommandType.ACCOUNT_INFO, account_id=account_id), timeout_seconds=15
    )
    return dict(result.data) if result.ok else {"attached": False, "error": result.error_message}
