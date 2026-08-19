"""Чаты, сценарии и правила (ТЗ §26)."""

from __future__ import annotations

import base64
import uuid
from typing import Any

from fastapi import APIRouter, Query, Response
from sqlalchemy import delete, func, select

from app.api.deps import CommandBusDep, CurrentUser, DbDep, OperatorUser
from app.bus.messages import Command, CommandType
from app.core.errors import AppError, InvalidInputError, NotFoundError
from app.database.repositories.chats import ChatRepository
from app.models import (
    Account,
    Chat,
    Lead,
    Message,
    ProcessedStatus,
    Rule,
    RuleAccount,
    RuleChat,
    Scenario,
)
from app.rules.keywords import KeywordSpec, compile_regex
from app.schemas.common import Ok
from app.schemas.resources import (
    ChatCreate,
    ChatNode,
    ChatOut,
    ChatTreeAccount,
    ChatUpdate,
    RuleCreate,
    RuleOut,
    RuleUpdate,
    ScenarioCreate,
    ScenarioOut,
    ScenarioUpdate,
)

chats_router = APIRouter(prefix="/chats", tags=["chats"])
scenarios_router = APIRouter(prefix="/scenarios", tags=["scenarios"])
rules_router = APIRouter(prefix="/rules", tags=["rules"])


# --- чаты ------------------------------------------------------------------
@chats_router.get("", response_model=list[ChatOut], summary="Список чатов")
async def list_chats(
    _user: CurrentUser,
    db: DbDep,
    account_id: uuid.UUID | None = Query(default=None),
    monitored: bool | None = Query(default=None),
) -> list[ChatOut]:
    stmt = select(Chat).order_by(Chat.title)
    if account_id is not None:
        stmt = stmt.where(Chat.account_id == account_id)
    if monitored is not None:
        stmt = stmt.where(Chat.monitored.is_(monitored))
    rows = await db.scalars(stmt)
    return [ChatOut.from_orm_chat(row) for row in rows.all()]


@chats_router.get("/tree", response_model=list[ChatTreeAccount], summary="Дерево чатов")
async def chats_tree(_user: CurrentUser, db: DbDep) -> list[ChatTreeAccount]:
    """Все чаты по аккаунтам со счётчиками: сообщения, лиды, ответы, участники.

    «Лиды» — сколько разных людей из чата попали в таблицу лидов; «ответы» —
    сколько раз мы туда написали; «участники» — сколько разных отправителей
    видели за всё время. Это и есть активность, о которой просили.
    """
    # Счётчики по сообщениям в разрезе чата — одним запросом, без N+1.
    msg_stmt = (
        select(
            Message.chat_id,
            func.count().label("total"),
            func.count(func.distinct(Message.sender_tg_id)).label("users"),
            func.count()
            .filter(Message.processed_status == ProcessedStatus.REPLIED)
            .label("replies"),
        )
        .where(Message.chat_id.is_not(None))
        .group_by(Message.chat_id)
    )
    msg_rows = {row.chat_id: row for row in (await db.execute(msg_stmt)).all()}

    # Лиды в разрезе аккаунта и исходного чата.
    lead_stmt = select(Lead.account_id, Lead.source_chat_id, func.count().label("leads")).group_by(
        Lead.account_id, Lead.source_chat_id
    )
    lead_rows = (await db.execute(lead_stmt)).all()
    leads_by_chat: dict[uuid.UUID, int] = {}
    leads_by_account: dict[uuid.UUID, int] = {}
    for row in lead_rows:
        leads_by_account[row.account_id] = leads_by_account.get(row.account_id, 0) + row.leads
        if row.source_chat_id is not None:
            leads_by_chat[row.source_chat_id] = row.leads

    accounts = list((await db.scalars(select(Account).order_by(Account.created_at))).all())
    chats = list(
        (await db.scalars(select(Chat).order_by(Chat.last_message_at.desc().nullslast()))).all()
    )

    tree: list[ChatTreeAccount] = []
    for account in accounts:
        nodes: list[ChatNode] = []
        total_messages = 0
        for chat in chats:
            if chat.account_id != account.id:
                continue
            stats = msg_rows.get(chat.id)
            messages_total = int(stats.total) if stats else 0
            total_messages += messages_total
            nodes.append(
                ChatNode(
                    id=chat.id,
                    tg_chat_id=chat.tg_chat_id,
                    type=chat.type,
                    title=chat.title,
                    username=chat.username,
                    monitored=chat.monitored,
                    has_avatar=chat.avatar is not None,
                    last_message_at=chat.last_message_at,
                    messages_total=messages_total,
                    leads_count=leads_by_chat.get(chat.id, 0),
                    replies_count=int(stats.replies) if stats else 0,
                    active_users=int(stats.users) if stats else 0,
                )
            )
        tree.append(
            ChatTreeAccount(
                account_id=account.id,
                label=account.label,
                username=account.username,
                messages_total=total_messages,
                leads_count=leads_by_account.get(account.id, 0),
                chats=nodes,
            )
        )
    return tree


@chats_router.get("/{chat_id}/avatar", summary="Аватар чата")
async def chat_avatar(
    chat_id: uuid.UUID, _user: CurrentUser, bus: CommandBusDep, db: DbDep
) -> Response:
    """Отдаёт кешированный аватар, при первом обращении просит воркер скачать.

    Прозрачный 1x1 PNG вместо 404, если фото нет: фронтенду проще показать
    заглушку через CSS, чем обрабатывать ошибку на каждой аватарке.
    """
    chat = await db.get(Chat, chat_id)
    if chat is None:
        raise NotFoundError("Чат не найден")

    if chat.avatar is None and chat.avatar_fetched_at is None:
        # Ещё не пробовали скачать — просим владельца аккаунта.
        try:
            result = await bus.call(
                Command(
                    type=CommandType.CHAT_PHOTO,
                    account_id=chat.account_id,
                    payload={"chat_id": chat.tg_chat_id},
                ),
                timeout_seconds=30,
            )
        except AppError:
            result = None

        data: bytes | None = None
        mime: str | None = None
        if result and result.ok and result.data.get("has_photo"):
            data = base64.b64decode(result.data["data"])
            mime = result.data.get("mime", "image/jpeg")
        await ChatRepository(db).set_avatar(chat.id, data, mime)
        chat.avatar, chat.avatar_mime = data, mime

    if chat.avatar is None:
        transparent = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMDAQD3o5m9AAAAAElFTkSuQmCC"
        )
        return Response(content=transparent, media_type="image/png")

    return Response(
        content=chat.avatar,
        media_type=chat.avatar_mime or "image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@chats_router.post("", response_model=ChatOut, status_code=201, summary="Добавить чат")
async def create_chat(payload: ChatCreate, _user: OperatorUser, db: DbDep) -> ChatOut:
    chat = await ChatRepository(db).ensure(
        payload.account_id,
        payload.tg_chat_id,
        chat_type=payload.type,
        title=payload.title,
        username=payload.username,
        monitored=payload.monitored,
    )
    chat.monitored = payload.monitored
    await db.flush()
    return ChatOut.from_orm_chat(chat)


@chats_router.patch("/{chat_id}", response_model=ChatOut, summary="Изменить чат")
async def update_chat(
    chat_id: uuid.UUID, payload: ChatUpdate, _user: OperatorUser, db: DbDep
) -> ChatOut:
    chat = await db.get(Chat, chat_id)
    if chat is None:
        raise NotFoundError("Чат не найден")
    if payload.monitored is not None:
        chat.monitored = payload.monitored
    if payload.title is not None:
        chat.title = payload.title
    await db.flush()
    return ChatOut.from_orm_chat(chat)


@chats_router.delete("/{chat_id}", response_model=Ok, summary="Убрать чат")
async def delete_chat(chat_id: uuid.UUID, _user: OperatorUser, db: DbDep) -> Ok:
    await db.execute(delete(Chat).where(Chat.id == chat_id))
    return Ok(detail="Чат удалён")


# --- сценарии --------------------------------------------------------------
@scenarios_router.get("", response_model=list[ScenarioOut], summary="Список сценариев")
async def list_scenarios(_user: CurrentUser, db: DbDep) -> list[ScenarioOut]:
    rows = await db.scalars(select(Scenario).order_by(Scenario.name))
    return [ScenarioOut.model_validate(row) for row in rows.all()]


@scenarios_router.post("", response_model=ScenarioOut, status_code=201, summary="Создать сценарий")
async def create_scenario(payload: ScenarioCreate, _user: OperatorUser, db: DbDep) -> ScenarioOut:
    scenario = Scenario(**payload.model_dump(exclude_none=True))
    db.add(scenario)
    await db.flush()
    return ScenarioOut.model_validate(scenario)


@scenarios_router.get("/{scenario_id}", response_model=ScenarioOut, summary="Сценарий")
async def get_scenario(scenario_id: uuid.UUID, _user: CurrentUser, db: DbDep) -> ScenarioOut:
    scenario = await db.get(Scenario, scenario_id)
    if scenario is None:
        raise NotFoundError("Сценарий не найден")
    return ScenarioOut.model_validate(scenario)


@scenarios_router.patch("/{scenario_id}", response_model=ScenarioOut, summary="Изменить сценарий")
async def update_scenario(
    scenario_id: uuid.UUID, payload: ScenarioUpdate, _user: OperatorUser, db: DbDep
) -> ScenarioOut:
    scenario = await db.get(Scenario, scenario_id)
    if scenario is None:
        raise NotFoundError("Сценарий не найден")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(scenario, field, value)
    await db.flush()
    return ScenarioOut.model_validate(scenario)


@scenarios_router.delete("/{scenario_id}", response_model=Ok, summary="Удалить сценарий")
async def delete_scenario(scenario_id: uuid.UUID, _user: OperatorUser, db: DbDep) -> Ok:
    await db.execute(delete(Scenario).where(Scenario.id == scenario_id))
    return Ok(detail="Сценарий удалён")


# --- правила ---------------------------------------------------------------
def _validate_rule(payload: RuleCreate | RuleUpdate) -> None:
    """Ошибки в правиле должны всплывать при сохранении, а не в конвейере.

    Кривое регулярное выражение — ошибка оператора, а не сбой сервера,
    поэтому ValueError превращается в понятный 422 с текстом причины.
    """
    try:
        if payload.keywords:
            KeywordSpec.from_dict(payload.keywords)
        if payload.regex:
            compile_regex(payload.regex)
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from exc

    if payload.ai_enabled and payload.ai_threshold is None:
        raise InvalidInputError("При включённом AI нужен порог уверенности")


async def _rule_links(db: DbDep, rule_id: uuid.UUID) -> dict[str, list[str]]:
    accounts = await db.scalars(
        select(RuleAccount.account_id).where(RuleAccount.rule_id == rule_id)
    )
    chats = await db.scalars(select(RuleChat.chat_id).where(RuleChat.rule_id == rule_id))
    return {
        "account_ids": [str(item) for item in accounts.all()],
        "chat_ids": [str(item) for item in chats.all()],
    }


async def _set_links(
    db: DbDep, rule_id: uuid.UUID, account_ids: list[uuid.UUID], chat_ids: list[uuid.UUID]
) -> None:
    await db.execute(delete(RuleAccount).where(RuleAccount.rule_id == rule_id))
    await db.execute(delete(RuleChat).where(RuleChat.rule_id == rule_id))
    for account_id in account_ids:
        db.add(RuleAccount(rule_id=rule_id, account_id=account_id))
    for chat_id in chat_ids:
        db.add(RuleChat(rule_id=rule_id, chat_id=chat_id))


@rules_router.get("", response_model=list[RuleOut], summary="Список правил")
async def list_rules(_user: CurrentUser, db: DbDep) -> list[RuleOut]:
    rows = await db.scalars(select(Rule).order_by(Rule.priority.desc(), Rule.name))
    return [RuleOut.model_validate(row) for row in rows.all()]


@rules_router.post("", response_model=RuleOut, status_code=201, summary="Создать правило")
async def create_rule(payload: RuleCreate, _user: OperatorUser, db: DbDep) -> RuleOut:
    _validate_rule(payload)
    data = payload.model_dump(exclude={"account_ids", "chat_ids"})
    rule = Rule(**data)
    db.add(rule)
    await db.flush()
    await _set_links(db, rule.id, payload.account_ids, payload.chat_ids)
    await db.flush()
    return RuleOut.model_validate(rule)


@rules_router.get("/{rule_id}", summary="Правило вместе с привязками")
async def get_rule(rule_id: uuid.UUID, _user: CurrentUser, db: DbDep) -> dict[str, Any]:
    rule = await db.get(Rule, rule_id)
    if rule is None:
        raise NotFoundError("Правило не найдено")
    return {
        **RuleOut.model_validate(rule).model_dump(mode="json"),
        **await _rule_links(db, rule_id),
    }


@rules_router.patch("/{rule_id}", response_model=RuleOut, summary="Изменить правило")
async def update_rule(
    rule_id: uuid.UUID, payload: RuleUpdate, _user: OperatorUser, db: DbDep
) -> RuleOut:
    rule = await db.get(Rule, rule_id)
    if rule is None:
        raise NotFoundError("Правило не найдено")
    _validate_rule(payload)

    fields = payload.model_dump(exclude_unset=True, exclude={"account_ids", "chat_ids"})
    for field, value in fields.items():
        setattr(rule, field, value)

    updated = payload.model_dump(exclude_unset=True)
    if "account_ids" in updated or "chat_ids" in updated:
        await _set_links(db, rule_id, payload.account_ids, payload.chat_ids)
    await db.flush()
    return RuleOut.model_validate(rule)


@rules_router.delete("/{rule_id}", response_model=Ok, summary="Удалить правило")
async def delete_rule(rule_id: uuid.UUID, _user: OperatorUser, db: DbDep) -> Ok:
    await db.execute(delete(Rule).where(Rule.id == rule_id))
    return Ok(detail="Правило удалено")
