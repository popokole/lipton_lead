"""Настройки бота-уведомлений (ТЗ §36).

Токен бота только принимается, но не отдаётся: в ответе лишь признак
`configured` и username. Проверка токена — через getMe перед сохранением.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import AdminUser, DbDep, RuntimeDep
from app.core.crypto import build_secret_box
from app.core.errors import InvalidInputError
from app.models.notify import SINGLETON_ID, NotifySettings
from app.notifications.notifier import NotifierBot, NotifyError

router = APIRouter(prefix="/notify", tags=["notify"])


class NotifyStatus(BaseModel):
    enabled: bool
    configured: bool
    group_id: int | None
    bot_username: str | None
    last_error: str | None


class NotifyUpdate(BaseModel):
    bot_token: str | None = Field(default=None, max_length=128)
    group_id: int | None = None
    enabled: bool | None = None


async def _get_or_create(db: DbDep) -> NotifySettings:
    row = await db.get(NotifySettings, SINGLETON_ID)
    if row is None:
        row = NotifySettings(id=SINGLETON_ID)
        db.add(row)
        await db.flush()
    return row


def _status(row: NotifySettings) -> NotifyStatus:
    return NotifyStatus(
        enabled=row.enabled,
        configured=row.bot_token_ct is not None,
        group_id=row.group_id,
        bot_username=row.bot_username,
        last_error=row.last_error,
    )


@router.get("", response_model=NotifyStatus, summary="Настройки бота-уведомлений")
async def get_notify(_admin: AdminUser, db: DbDep) -> NotifyStatus:
    return _status(await _get_or_create(db))


@router.put("", response_model=NotifyStatus, summary="Сохранить настройки")
async def update_notify(
    payload: NotifyUpdate, _admin: AdminUser, runtime: RuntimeDep, db: DbDep
) -> NotifyStatus:
    row = await _get_or_create(db)

    if payload.bot_token:
        # Проверяем токен и заодно узнаём username бота.
        notifier = NotifierBot(build_secret_box(runtime.settings), proxy=runtime.settings.ai_proxy_url)
        try:
            username = await notifier.check(payload.bot_token)
        except NotifyError as exc:
            raise InvalidInputError(f"Токен не принят Telegram: {exc}") from exc
        finally:
            await notifier.close()

        blob = build_secret_box(runtime.settings).encrypt(payload.bot_token, aad="notify")
        row.bot_token_ct = blob.ciphertext
        row.bot_token_nonce = blob.nonce
        row.bot_token_key_id = blob.key_id
        row.bot_username = username
        row.last_error = None

    if payload.group_id is not None:
        row.group_id = payload.group_id
    if payload.enabled is not None:
        row.enabled = payload.enabled

    await db.flush()
    return _status(row)


@router.post("/test", summary="Проверить связь с ботом")
async def test_notify(_admin: AdminUser, runtime: RuntimeDep, db: DbDep) -> dict[str, str]:
    row = await _get_or_create(db)
    if not (row.bot_token_ct and row.bot_token_nonce and row.bot_token_key_id):
        raise InvalidInputError("Токен ещё не сохранён")

    from app.core.crypto import EncryptedBlob

    box = build_secret_box(runtime.settings)
    token = box.decrypt_str(
        EncryptedBlob(row.bot_token_ct, row.bot_token_nonce, row.bot_token_key_id, "AES-256-GCM"),
        aad="notify",
    )
    notifier = NotifierBot(box, proxy=runtime.settings.ai_proxy_url)
    try:
        username = await notifier.check(token)
    except NotifyError as exc:
        raise InvalidInputError(str(exc)) from exc
    finally:
        await notifier.close()
    return {"ok": "true", "bot_username": username}
