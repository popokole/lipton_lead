"""Прокси для подключения аккаунтов (ТЗ §31).

Прокси здесь — способ дотянуться до Telegram из сети, где он недоступен
напрямую. Это статичная настройка аккаунта: ротации прокси между аккаунтами
нет и не будет, она нужна только для обхода ограничений, а не для работы.

Пароль прокси хранится зашифрованным тем же ключом, что и сессии, и наружу не
отдаётся — в `ProxyOut` для него просто нет поля.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter
from sqlalchemy import delete, select

from app.api.deps import CurrentUser, DbDep, OperatorUser, RuntimeDep
from app.core.crypto import build_secret_box
from app.models import Proxy
from app.schemas.common import Ok
from app.schemas.resources import ProxyCreate, ProxyOut

router = APIRouter(prefix="/proxies", tags=["proxies"])


@router.get("", response_model=list[ProxyOut], summary="Список прокси")
async def list_proxies(_user: CurrentUser, db: DbDep) -> list[ProxyOut]:
    rows = await db.scalars(select(Proxy).order_by(Proxy.name))
    return [ProxyOut.model_validate(row) for row in rows.all()]


@router.post("", response_model=ProxyOut, status_code=201, summary="Добавить прокси")
async def create_proxy(
    payload: ProxyCreate, _user: OperatorUser, runtime: RuntimeDep, db: DbDep
) -> ProxyOut:
    proxy = Proxy(
        name=payload.name,
        scheme=payload.scheme,
        host=payload.host,
        port=payload.port,
        username=payload.username,
        enabled=payload.enabled,
    )

    if payload.password:
        blob = build_secret_box(runtime.settings).encrypt(payload.password, aad="proxy")
        proxy.password_ct = blob.ciphertext
        proxy.password_nonce = blob.nonce
        proxy.password_key_id = blob.key_id

    db.add(proxy)
    await db.flush()
    return ProxyOut.model_validate(proxy)


@router.delete("/{proxy_id}", response_model=Ok, summary="Удалить прокси")
async def delete_proxy(proxy_id: uuid.UUID, _user: OperatorUser, db: DbDep) -> Ok:
    # У аккаунтов ссылка обнулится каскадом (ON DELETE SET NULL).
    await db.execute(delete(Proxy).where(Proxy.id == proxy_id))
    return Ok(detail="Прокси удалён")
