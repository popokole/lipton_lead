"""Импорт готовых сессий: tdata и файл .session (ТЗ §4).

Это второй способ подключить аккаунт — для тех, кто уже вошёл в Telegram
Desktop и не хочет проходить вход по коду заново.

Импортировать можно только свои собственные аккаунты: это перенос доступа,
который у вас уже есть.

Конвертация выполняется прямо здесь, в API, и это не противоречит правилу
«клиент только в воркере»: при `UseCurrentSession` opentele не делает ни
одного сетевого запроса, а просто пересобирает ключ авторизации в строку.
Подключается к Telegram уже воркер — после того, как заберёт аккаунт в аренду.

Распакованная tdata живёт на диске ровно столько, сколько идёт конвертация:
в ней лежат ключи в открытом виде.
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, UploadFile

from app.api.deps import DbDep, OperatorUser, RuntimeDep
from app.core.crypto import build_secret_box
from app.core.errors import InvalidInputError, NotFoundError
from app.core.logging import get_logger
from app.database.repositories.accounts import AccountRepository
from app.models import AccountStatus
from app.schemas.resources import AccountOut, TDataAccountOut
from app.telegram.adapters import tdata
from app.telegram.session_manager import SessionManager, StringSessionAdapter

logger = get_logger(__name__)

router = APIRouter(prefix="/imports", tags=["imports"])


async def _collect(files: list[UploadFile], limit_bytes: int) -> list[tuple[str, bytes]]:
    """Считывает загрузку в память, следя за общим размером."""
    collected: list[tuple[str, bytes]] = []
    total = 0
    for item in files:
        content = await item.read()
        total += len(content)
        if total > limit_bytes:
            raise InvalidInputError(f"Загрузка больше допустимых {limit_bytes // (1024 * 1024)} МБ")
        collected.append((item.filename or "file", content))
    if not collected:
        raise InvalidInputError("Файлы не переданы")
    return collected


@router.post(
    "/tdata/inspect",
    response_model=list[TDataAccountOut],
    summary="Показать аккаунты внутри tdata",
)
async def inspect_tdata(
    runtime: RuntimeDep,
    _user: OperatorUser,
    files: list[UploadFile] = File(...),
) -> list[TDataAccountOut]:
    """В одной папке Desktop может быть несколько аккаунтов — даём выбрать."""
    payload = await _collect(files, runtime.settings.tdata_max_upload_bytes)

    workdir = Path(tempfile.mkdtemp(prefix="tdata-"))
    try:
        tdata_dir = tdata.materialize_upload(payload, workdir)
        accounts = tdata.list_accounts(tdata_dir)
    finally:
        tdata.cleanup(workdir)

    logger.info("tdata_inspected", accounts=len(accounts))
    return [
        TDataAccountOut(index=item.index, tg_user_id=item.tg_user_id, dc_id=item.dc_id)
        for item in accounts
    ]


@router.post(
    "/tdata/{account_id}",
    response_model=AccountOut,
    summary="Импортировать аккаунт из tdata",
)
async def import_tdata(
    account_id: uuid.UUID,
    runtime: RuntimeDep,
    db: DbDep,
    _user: OperatorUser,
    files: list[UploadFile] = File(...),
    account_index: int | None = Form(default=None),
) -> AccountOut:
    repository = AccountRepository(db)
    account = await repository.get(account_id)
    if account is None:
        raise NotFoundError("Аккаунт не найден", account_id=str(account_id))

    payload = await _collect(files, runtime.settings.tdata_max_upload_bytes)

    workdir = Path(tempfile.mkdtemp(prefix="tdata-"))
    try:
        tdata_dir = tdata.materialize_upload(payload, workdir)
        found = tdata.list_accounts(tdata_dir)
        chosen = _choose(found, account_index)
        credentials = await tdata.to_credentials(tdata_dir, chosen.index)
    finally:
        tdata.cleanup(workdir)

    manager = SessionManager(build_secret_box(runtime.settings))
    await manager.store(
        db,
        account_id,
        session_string=credentials.session_string,
        api_id=credentials.api_id,
        api_hash=credentials.api_hash,
    )
    await repository.update_identity(
        account_id,
        tg_user_id=chosen.tg_user_id,
        username=account.username,
        display_name=account.display_name,
    )
    # Дальше аккаунт подхватит воркер: подключение — его работа, не API.
    await repository.set_status(account_id, AccountStatus.OFFLINE)
    await db.flush()

    refreshed = await repository.get(account_id)
    assert refreshed is not None
    logger.info("tdata_imported", account_id=str(account_id), tg_user_id=chosen.tg_user_id)
    return AccountOut.model_validate(refreshed)


@router.post(
    "/session-file/{account_id}",
    response_model=AccountOut,
    summary="Импортировать файл .session",
)
async def import_session_file(
    account_id: uuid.UUID,
    runtime: RuntimeDep,
    db: DbDep,
    _user: OperatorUser,
    file: UploadFile = File(...),
    api_id: int = Form(...),
    api_hash: str = Form(...),
) -> AccountOut:
    """Файл .session не хранит api_id/api_hash — их придётся указать отдельно."""
    repository = AccountRepository(db)
    account = await repository.get(account_id)
    if account is None:
        raise NotFoundError("Аккаунт не найден", account_id=str(account_id))

    content = await file.read()
    if len(content) > runtime.settings.tdata_max_upload_bytes:
        raise InvalidInputError("Файл слишком большой для сессии Telethon")

    from app.telegram.session_manager import FileSessionAdapter

    try:
        session_string = FileSessionAdapter().to_string(content)
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from exc

    if not StringSessionAdapter().validate(session_string):
        raise InvalidInputError("Из файла не удалось собрать корректную сессию")

    manager = SessionManager(build_secret_box(runtime.settings))
    await manager.store(
        db, account_id, session_string=session_string, api_id=api_id, api_hash=api_hash
    )
    await repository.set_status(account_id, AccountStatus.OFFLINE)
    await db.flush()

    refreshed = await repository.get(account_id)
    assert refreshed is not None
    logger.info("session_file_imported", account_id=str(account_id))
    return AccountOut.model_validate(refreshed)


def _choose(found: list[tdata.TDataAccount], index: int | None) -> tdata.TDataAccount:
    if not found:
        raise InvalidInputError("В tdata не нашлось ни одного аккаунта")
    if index is None:
        return found[0]
    chosen = next((item for item in found if item.index == index), None)
    if chosen is None:
        raise InvalidInputError(
            f"В tdata нет аккаунта с номером {index}",
            available=[item.index for item in found],
        )
    return chosen
