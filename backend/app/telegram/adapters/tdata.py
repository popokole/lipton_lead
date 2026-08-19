"""Импорт tdata из Telegram Desktop (ТЗ §4, adapter layer).

Внутренний формат системы остаётся один — StringSession. Этот модуль лишь
переводит в него внешний формат и внутрь конвейера не проникает: всё, что
дальше, работает с обычной строкой сессии.

Импортировать можно только tdata своих собственных аккаунтов. Это перенос
доступа, который у вас уже есть, а не способ получить чужой.

Конвертация выполняется полностью офлайн, без единого запроса в Telegram.
Это важно по двум причинам: живой клиент по нашей архитектуре бывает только в
воркере, и вход в Desktop не должен слетать от того, что мы прочитали папку.
Флаг `UseCurrentSession` означает «взять существующий ключ авторизации», а не
«войти заново», — поэтому исходный Telegram Desktop продолжает работать.

Почему такая странная установка зависимостей (см. Dockerfile): opentele
работает поверх Qt и требует `tgcrypto` — C-расширения, у которого нет колёс
под Python 3.12. Поэтому opentele ставится без зависимостей, а вместо
tgcrypto подкладывается чистый Python-эквивалент из `vendor/`.
"""

from __future__ import annotations

import contextlib
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.errors import InvalidInputError
from app.core.logging import get_logger
from app.telegram.session_manager import SessionCredentials

logger = get_logger(__name__)

# Файл-карта аккаунтов внутри папки tdata. По нему её и опознаём.
TDATA_MARKER = "key_datas"

_RUNTIME_PATCHED = False


@dataclass(frozen=True, slots=True)
class TDataAccount:
    """Один аккаунт внутри tdata: в папке Desktop их может быть несколько."""

    index: int
    tg_user_id: int
    dc_id: int


def _patch_opentele_runtime() -> None:
    """Две правки совместимости, без которых свежие tdata не читаются.

    1. Новые версии Telegram Desktop пишут в карту ключи типов, которых
       opentele не знает (кастомные эмодзи, токены webview), и падает на всей
       папке. Ключи авторизации лежат в отдельном файле, поэтому ошибку чтения
       карты считаем некритичной и продолжаем читать MTP-данные.
    2. Сеттер `Account.api` в opentele синхронизирует значение обратно во
       владельца и зацикливается, когда в профиле больше одного аккаунта.

    Обе взяты из прототипа tg-monitor, где проверены на реальных папках.
    """
    global _RUNTIME_PATCHED
    if _RUNTIME_PATCHED:
        return

    import opentele.td.account as account_module
    from opentele.exception import OpenTeleException
    from PyQt5.QtCore import QByteArray

    def read_map_with(
        self: Any, local_key: Any, legacy_passcode: QByteArray = QByteArray()
    ) -> None:
        # Неизвестные новые ключи карты пропускаем: авторизация лежит отдельно.
        with contextlib.suppress(OpenTeleException):
            self.mapData.read(local_key, legacy_passcode)
        self.readMtpData()

    account_module.StorageAccount.readMapWith = read_map_with
    account_module.Account.api = property(
        lambda self: getattr(self, "_Account__api", None),
        lambda self, value: setattr(self, "_Account__api", value),
    )
    _RUNTIME_PATCHED = True


def find_tdata_dir(root: Path) -> Path:
    """Находит настоящую папку tdata внутри распакованной загрузки.

    Пользователь может прислать и саму папку, и архив с ней, и папку профиля
    Telegram Desktop целиком — искать нужно во всех случаях.
    """
    for path in [root, *root.rglob("*")]:
        if path.is_dir() and (path / TDATA_MARKER).exists():
            return path
    for path in [root, *root.rglob("tdata")]:
        if path.is_dir() and path.name == "tdata":
            return path
    raise InvalidInputError(
        "В загруженных файлах нет папки tdata — нужна папка целиком "
        f"(в ней должен быть файл {TDATA_MARKER})"
    )


def materialize_upload(files: list[tuple[str, bytes]], workdir: Path) -> Path:
    """Раскладывает загруженные файлы на диск и возвращает папку tdata.

    Пути внутри архива сохраняются: tdata — это набор файлов, где важны и
    имена, и вложенность.
    """
    for relative, content in files:
        safe = Path(relative.replace("\\", "/").lstrip("/"))
        # Защита от путей вида ../../etc: раскладываем строго внутрь workdir.
        destination = (workdir / safe).resolve()
        if not str(destination).startswith(str(workdir.resolve())):
            raise InvalidInputError(f"Недопустимый путь в загрузке: {relative}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)

    for archive in list(workdir.rglob("*.zip")):
        try:
            with zipfile.ZipFile(archive) as zip_file:
                zip_file.extractall(workdir)
        except zipfile.BadZipFile as exc:
            raise InvalidInputError(f"Архив {archive.name} повреждён") from exc

    return find_tdata_dir(workdir)


def open_tdesktop(tdata_dir: Path) -> Any:
    """Читает папку tdata. Ошибки opentele переводит в понятные оператору."""
    _patch_opentele_runtime()
    from opentele.td import TDesktop

    try:
        # opentele бросает наследников BaseException, поэтому ловим широко.
        desktop = TDesktop(str(tdata_dir))
    except BaseException as exc:  # noqa: BLE001
        raise InvalidInputError(f"Не удалось прочитать tdata: {exc}") from None

    if not desktop.isLoaded():
        raise InvalidInputError(
            "tdata не загрузилась: папка неполная или закрыта локальным паролем Telegram Desktop"
        )
    return desktop


def list_accounts(tdata_dir: Path) -> list[TDataAccount]:
    """Аккаунты внутри папки. В Desktop их может быть до шести."""
    desktop = open_tdesktop(tdata_dir)
    return [
        TDataAccount(
            index=int(account.index), tg_user_id=int(account.UserId), dc_id=int(account.MainDcId)
        )
        for account in desktop.accounts
    ]


async def to_credentials(tdata_dir: Path, account_index: int | None = None) -> SessionCredentials:
    """Переводит выбранный аккаунт из tdata в StringSession.

    Ни одного сетевого запроса здесь не происходит: `UseCurrentSession`
    переиспользует уже имеющийся ключ авторизации, а строку сессии мы собираем
    сами из ключа и номера дата-центра.
    """
    from opentele.api import UseCurrentSession
    from telethon.sessions import StringSession

    desktop = open_tdesktop(tdata_dir)

    target: Any = desktop
    if account_index is not None:
        target = next((item for item in desktop.accounts if int(item.index) == account_index), None)
        if target is None:
            raise InvalidInputError(
                f"В tdata нет аккаунта с номером {account_index}",
                available=[int(item.index) for item in desktop.accounts],
            )

    try:
        import opentele.tl as opentele_telethon

        # session=None — opentele соберёт сессию в памяти; передавать сюда
        # StringSession нельзя, на этом у него ошибка.
        client = await opentele_telethon.TelegramClient.FromTDesktop(
            target, session=None, flag=UseCurrentSession
        )
    except BaseException as exc:  # noqa: BLE001 — opentele бросает не только Exception
        raise InvalidInputError(f"Не удалось преобразовать tdata: {exc}") from None

    session = client.session
    if session.auth_key is None:
        raise InvalidInputError("В tdata нет ключа авторизации — аккаунт не был залогинен")

    string_session = StringSession()
    string_session.set_dc(session.dc_id, session.server_address, session.port)
    string_session.auth_key = session.auth_key

    api_id, api_hash = _extract_api(client, target, desktop)

    logger.info("tdata_converted", dc_id=session.dc_id, account_index=account_index)
    return SessionCredentials(
        session_string=string_session.save(), api_id=int(api_id), api_hash=str(api_hash)
    )


def _extract_api(client: Any, target: Any, desktop: Any) -> tuple[int, str]:
    """Достаёт api_id/api_hash, которыми была создана сессия.

    Менять их при импорте нельзя: ключ авторизации выдан конкретному
    приложению, и подключение с чужим api_id к чужой сессии выглядит для
    Telegram как угон. Поэтому переносим ровно то, что записано в tdata.

    Источники перебираются по убыванию достоверности: сначала сам аккаунт,
    потом профиль Desktop, и только затем собранный клиент — у Telethon поля
    называются `api_id`/`api_hash`, без подчёркивания.
    """
    candidates = [getattr(target, "api", None), getattr(desktop, "api", None), client]
    for source in candidates:
        if source is None:
            continue
        api_id = getattr(source, "api_id", None)
        api_hash = getattr(source, "api_hash", None)
        if api_id and api_hash:
            return int(api_id), str(api_hash)

    raise InvalidInputError(
        "В tdata не нашлись api_id и api_hash — папка неполная или от очень "
        "старой версии Telegram Desktop"
    )


def cleanup(workdir: Path) -> None:
    """Удаляет распакованную tdata.

    Обязательный шаг: во временной папке лежат ключи авторизации в открытом
    виде, и оставлять их на диске дольше самой конвертации нельзя.
    """
    shutil.rmtree(workdir, ignore_errors=True)
