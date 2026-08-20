"""Сообщения, действия, журнал, диалоги и лиды (ТЗ §26)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import Select, and_, exists, func, literal, select
from sqlalchemy import delete as sa_delete
from sqlalchemy import update as sa_update

from app.api.deps import CommandBusDep, CurrentUser, DbDep, OperatorUser, RedisDep
from app.bus.events import EventPublisher
from app.bus.messages import Command, CommandType, Event
from app.bus.messages import EventType as BusEventType
from app.core.clock import utcnow
from app.core.errors import AppError, InvalidInputError, NotFoundError
from app.database.repositories.conversations import ConversationRepository
from app.database.repositories.events import EventLogRepository
from app.models import (
    Account,
    Action,
    ActionStatus,
    Chat,
    ChatType,
    Conversation,
    ConversationStatus,
    EventLog,
    EventType,
    Lead,
    Message,
    Notification,
    NotificationType,
    ProcessedStatus,
    Rule,
    RuleChat,
    Scenario,
)
from app.models.enums import ActionType, RuleScope
from app.schemas.common import Ok, Page
from app.schemas.resources import (
    ActionOut,
    ConversationOut,
    EscalationOut,
    EventLogOut,
    LeadOut,
    MessageOut,
    ThreadOut,
)

messages_router = APIRouter(prefix="/messages", tags=["messages"])
actions_router = APIRouter(prefix="/actions", tags=["actions"])
logs_router = APIRouter(prefix="/logs", tags=["logs"])
conversations_router = APIRouter(prefix="/conversations", tags=["conversations"])
leads_router = APIRouter(prefix="/leads", tags=["leads"])
handoff_router = APIRouter(prefix="/handoff", tags=["handoff"])


async def _count(db: DbDep, stmt: Select[Any]) -> int:
    """Общее число строк для постраничного вывода."""
    subquery = stmt.order_by(None).subquery()
    return int(await db.scalar(select(func.count()).select_from(subquery)) or 0)


@messages_router.get("", response_model=Page[MessageOut], summary="Сообщения")
async def list_messages(
    _user: CurrentUser,
    db: DbDep,
    account_id: uuid.UUID | None = Query(default=None),
    chat_id: uuid.UUID | None = Query(default=None),
    matched_only: bool = Query(default=False),
    search: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[MessageOut]:
    # Канал (название и наличие аватара) берём джойном к чату — иначе на
    # каждое сообщение был бы отдельный запрос за его чатом.
    stmt = (
        select(
            Message,
            Chat.title.label("chat_title"),
            Chat.username.label("chat_username"),
            (Chat.avatar.is_not(None)).label("chat_has_avatar"),
        )
        .outerjoin(Chat, Chat.id == Message.chat_id)
        .order_by(Message.date.desc())
    )
    if account_id is not None:
        stmt = stmt.where(Message.account_id == account_id)
    if chat_id is not None:
        stmt = stmt.where(Message.chat_id == chat_id)
    if matched_only:
        stmt = stmt.where(Message.rule_id.is_not(None))
    if search:
        stmt = stmt.where(Message.text.ilike(f"%{search}%"))

    count_stmt = select(Message.id).select_from(Message).order_by(None)
    if account_id is not None:
        count_stmt = count_stmt.where(Message.account_id == account_id)
    if chat_id is not None:
        count_stmt = count_stmt.where(Message.chat_id == chat_id)
    if matched_only:
        count_stmt = count_stmt.where(Message.rule_id.is_not(None))
    if search:
        count_stmt = count_stmt.where(Message.text.ilike(f"%{search}%"))
    total = await _count(db, count_stmt)

    items: list[MessageOut] = []
    for row in (await db.execute(stmt.limit(limit).offset(offset))).all():
        message = MessageOut.model_validate(row[0])
        items.append(
            message.model_copy(
                update={
                    "chat_title": row.chat_title,
                    "chat_username": row.chat_username,
                    "chat_has_avatar": bool(row.chat_has_avatar),
                }
            )
        )
    return Page(items=items, total=total, limit=limit, offset=offset)


@actions_router.get("", response_model=Page[ActionOut], summary="Действия")
async def list_actions(
    _user: CurrentUser,
    db: DbDep,
    account_id: uuid.UUID | None = Query(default=None),
    status: ActionStatus | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[ActionOut]:
    stmt = select(Action).order_by(Action.created_at.desc())
    if account_id is not None:
        stmt = stmt.where(Action.account_id == account_id)
    if status is not None:
        stmt = stmt.where(Action.status == status)

    total = await _count(db, stmt)
    rows = await db.scalars(stmt.limit(limit).offset(offset))
    return Page(
        items=[ActionOut.model_validate(row) for row in rows.all()],
        total=total,
        limit=limit,
        offset=offset,
    )


@logs_router.get("", response_model=Page[EventLogOut], summary="Журнал обработки")
async def list_logs(
    _user: CurrentUser,
    db: DbDep,
    account_id: uuid.UUID | None = Query(default=None),
    event_type: EventType | None = Query(default=None),
    level: str | None = Query(default=None, max_length=16),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> Page[EventLogOut]:
    base = select(EventLog).order_by(EventLog.ts.desc())
    if account_id is not None:
        base = base.where(EventLog.account_id == account_id)
    if event_type is not None:
        base = base.where(EventLog.event_type == event_type)
    if level:
        base = base.where(EventLog.level == level.upper())

    total = await _count(
        db,
        select(EventLog.id).order_by(None).where(base.whereclause)
        if base.whereclause is not None
        else select(EventLog.id),
    )

    enriched = (
        select(
            EventLog,
            Chat.title.label("chat_title"),
            Chat.username.label("chat_username"),
            (Chat.avatar.is_not(None)).label("chat_has_avatar"),
        )
        .select_from(EventLog)
        .outerjoin(Chat, Chat.id == EventLog.chat_id)
        .order_by(EventLog.ts.desc())
    )
    if base.whereclause is not None:
        enriched = enriched.where(base.whereclause)

    items: list[EventLogOut] = []
    for row in (await db.execute(enriched.limit(limit).offset(offset))).all():
        item = EventLogOut.model_validate(row[0])
        items.append(
            item.model_copy(
                update={
                    "chat_title": row.chat_title,
                    "chat_username": row.chat_username,
                    "chat_has_avatar": bool(row.chat_has_avatar),
                }
            )
        )
    return Page(items=items, total=total, limit=limit, offset=offset)


@conversations_router.get("", response_model=list[ConversationOut], summary="Диалоги")
async def list_conversations(
    _user: CurrentUser,
    db: DbDep,
    account_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[ConversationOut]:
    stmt = select(Conversation).order_by(Conversation.last_message_at.desc().nullslast())
    if account_id is not None:
        stmt = stmt.where(Conversation.account_id == account_id)
    rows = await db.scalars(stmt.limit(limit))
    return [ConversationOut.model_validate(row) for row in rows.all()]


_GROUP_TYPES = (ChatType.GROUP, ChatType.SUPERGROUP)

# Единое правило разговорного режима в группах: действует только в тех чатах,
# где оператор включил кнопку в «Общении» (привязка через rule_chats). ИИ сам
# решает, стоит ли отвечать, — не на всё подряд.
GROUP_CHAT_RULE_NAME = "Группа — общение ИИ"
GROUP_CHAT_SCENARIO_NAME = "Групповое общение"
GROUP_CHAT_PROMPT = (
    "ты обычный участник группового чата и пишешь как живой человек. вступай в "
    "разговор только когда есть что сказать по делу или можешь естественно "
    "поддержать беседу. не отвечай на всё подряд, не спамь, не повторяйся. "
    "коротко, с маленькой буквы, без официоза, без длинных тире."
)


async def _get_group_chat_rule(db: DbDep, *, create: bool) -> Rule | None:
    """Возвращает (создавая при create=True) правило разговорного режима групп."""
    rule = await db.scalar(select(Rule).where(Rule.name == GROUP_CHAT_RULE_NAME))
    if rule is not None or not create:
        return rule
    scenario = await db.scalar(select(Scenario).where(Scenario.name == GROUP_CHAT_SCENARIO_NAME))
    if scenario is None:
        scenario = Scenario(
            name=GROUP_CHAT_SCENARIO_NAME,
            system_prompt=GROUP_CHAT_PROMPT,
            human_handoff_enabled=False,
            require_knowledge_grounding=False,
            reply_in_dm=False,
            max_reply_length=250,
            context_messages=12,
            enabled=True,
        )
        db.add(scenario)
        await db.flush()
    rule = Rule(
        name=GROUP_CHAT_RULE_NAME,
        enabled=False,  # включается, как только появится первый привязанный чат
        priority=20,  # ниже тематических правил (1000): их темы имеют приоритет
        stop_on_match=True,
        scope=RuleScope.CHAT_MONITOR,
        scenario_id=scenario.id,
        keywords={},
        filters={},
        regex=None,
        ai_enabled=True,  # ИИ решает, отвечать ли — «по уму», не на всё подряд
        ai_threshold=0.5,
        cooldown={"user": 30},
        action=ActionType.REPLY,
        action_config={},
    )
    db.add(rule)
    await db.flush()
    return rule


@conversations_router.get("/threads", response_model=list[ThreadOut], summary="Треды общения")
async def list_threads(
    _user: CurrentUser,
    db: DbDep,
    kind: str = Query(default="dm", pattern="^(dm|group)$"),
    account_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
) -> list[ThreadOut]:
    """Треды общения по ЧАТАМ: отдельно личка (kind=dm), отдельно группы.

    Тред привязан к чату, а не к человеку, поэтому сообщения из группы и из
    лички не смешиваются. Показываем только чаты, где есть сообщения. Для лички
    подтягиваем балл лида, если собеседник — лид. Каналы (вещание) исключены.
    """
    types = list(_GROUP_TYPES) if kind == "group" else [ChatType.PRIVATE]

    # Признак «включён режим общения ИИ» для группы = есть привязка чата к
    # правилу разговорного режима групп.
    group_rule_id = None
    if kind == "group":
        group_rule_id = await db.scalar(select(Rule.id).where(Rule.name == GROUP_CHAT_RULE_NAME))
    ai_chat_col: Any = (
        exists(
            select(RuleChat.rule_id).where(
                RuleChat.rule_id == group_rule_id, RuleChat.chat_id == Chat.id
            )
        )
        if group_rule_id is not None
        else literal(False)
    )

    last_msg = (
        select(Message.text, Message.is_incoming)
        .where(Message.chat_id == Chat.id)
        .order_by(Message.date.desc())
        .limit(1)
        .correlate(Chat)
    )
    last_text = last_msg.with_only_columns(Message.text).scalar_subquery()
    last_incoming = last_msg.with_only_columns(Message.is_incoming).scalar_subquery()
    msg_count = (
        select(func.count())
        .select_from(Message)
        .where(Message.chat_id == Chat.id)
        .correlate(Chat)
        .scalar_subquery()
    )
    has_messages = exists(select(Message.id).where(Message.chat_id == Chat.id))

    # Берём только нужные скалярные колонки, не сущность Chat целиком: иначе
    # SQLAlchemy тянет из БД бинарь аватара (LargeBinary) на каждую строку ради
    # одного булева has_avatar. Тот же приём — в list_messages/list_logs.
    stmt = (
        select(
            Chat.id.label("chat_id"),
            Chat.account_id.label("account_id"),
            Chat.tg_chat_id.label("tg_chat_id"),
            Chat.type.label("chat_type"),
            Chat.title.label("title"),
            Chat.username.label("username"),
            Chat.monitored.label("monitored"),
            Chat.last_message_at.label("last_message_at"),
            (Chat.avatar.is_not(None)).label("has_avatar"),
            last_text.label("last_text"),
            func.coalesce(last_incoming, False).label("awaiting_reply"),
            msg_count.label("mcount"),
            Lead.score.label("lead_score"),
            Lead.status.label("lead_status"),
            ai_chat_col.label("ai_chat"),
        )
        # Лид присоединяется только для лички: в группе «лид = чат» смысла нет.
        .outerjoin(
            Lead,
            and_(
                Chat.type == ChatType.PRIVATE,
                Lead.account_id == Chat.account_id,
                Lead.tg_user_id == Chat.tg_chat_id,
            ),
        )
        .where(Chat.type.in_(types), has_messages)
        .order_by(Chat.last_message_at.desc().nullslast())
    )
    if account_id is not None:
        stmt = stmt.where(Chat.account_id == account_id)

    threads: list[ThreadOut] = []
    for row in (await db.execute(stmt.limit(limit))).all():
        threads.append(
            ThreadOut(
                chat_id=row.chat_id,
                account_id=row.account_id,
                tg_chat_id=row.tg_chat_id,
                kind="dm" if row.chat_type == ChatType.PRIVATE else "group",
                title=row.title,
                username=row.username,
                has_avatar=bool(row.has_avatar),
                monitored=row.monitored,
                message_count=int(row.mcount or 0),
                last_message_at=row.last_message_at,
                last_text=row.last_text,
                awaiting_reply=bool(row.awaiting_reply),
                lead_score=row.lead_score,
                lead_status=row.lead_status,
                ai_chat=bool(row.ai_chat),
            )
        )
    return threads


@conversations_router.get(
    "/{chat_id}/messages", response_model=list[MessageOut], summary="Переписка чата"
)
async def thread_messages(
    chat_id: uuid.UUID,
    _user: CurrentUser,
    db: DbDep,
    limit: int = Query(default=200, ge=1, le=500),
) -> list[MessageOut]:
    chat = await db.get(Chat, chat_id)
    if chat is None:
        raise NotFoundError("Чат не найден")

    # Сообщения строго одного чата — личка и группы не смешиваются. Берём
    # последние `limit` (по убыванию) и разворачиваем в хронологию.
    rows = await db.scalars(
        select(Message)
        .where(Message.chat_id == chat_id)
        .order_by(Message.date.desc())
        .limit(limit)
    )
    items = [MessageOut.model_validate(message) for message in rows.all()]
    items.reverse()
    return items


class ThreadReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    reply_to: int | None = None


@conversations_router.post(
    "/{chat_id}/reply", response_model=MessageOut, summary="Ответить в чат"
)
async def thread_reply(
    chat_id: uuid.UUID,
    payload: ThreadReplyIn,
    _user: OperatorUser,
    bus: CommandBusDep,
    db: DbDep,
) -> MessageOut:
    """Оператор пишет в чат вручную от имени аккаунта (личка или группа).

    Отправку выполняет воркер-владелец аккаунта (единственный держатель
    TelegramClient). Отправленное помечаем `is_bot_reply=True`, чтобы конвейер
    не принял его за входящее и аккаунт не начал отвечать сам себе.
    """
    chat = await db.get(Chat, chat_id)
    if chat is None:
        raise NotFoundError("Чат не найден")

    command_payload: dict[str, Any] = {"chat_id": chat.tg_chat_id, "text": payload.text}
    if payload.reply_to is not None:
        command_payload["reply_to"] = payload.reply_to

    try:
        result = await bus.call(
            Command(
                type=CommandType.SEND_MESSAGE,
                account_id=chat.account_id,
                payload=command_payload,
            ),
            timeout_seconds=60,
        )
    except AppError as exc:
        raise InvalidInputError(f"Не удалось отправить: {exc}") from exc
    if not result.ok:
        raise InvalidInputError(result.error_message or "Воркер не смог отправить сообщение")

    now = utcnow()
    message = Message(
        account_id=chat.account_id,
        chat_id=chat.id,
        tg_chat_id=chat.tg_chat_id,
        tg_message_id=int(result.data["tg_message_id"]),
        text=payload.text,
        date=now,
        reply_to_tg_message_id=payload.reply_to,
        is_incoming=False,
        is_outgoing=True,
        is_bot_reply=True,
        processed_status=ProcessedStatus.REPLIED,
    )
    db.add(message)
    chat.last_message_at = now
    # В личке собеседник — лид: отмечаем активность.
    if chat.type is ChatType.PRIVATE:
        await db.execute(
            sa_update(Lead)
            .where(Lead.account_id == chat.account_id, Lead.tg_user_id == chat.tg_chat_id)
            .values(last_seen_at=now)
        )
    await db.flush()
    return MessageOut.model_validate(message)


class AiChatIn(BaseModel):
    enabled: bool


@conversations_router.post(
    "/{chat_id}/ai-chat", summary="Режим общения ИИ в группе (вкл/выкл)"
)
async def toggle_ai_chat(
    chat_id: uuid.UUID, payload: AiChatIn, _user: OperatorUser, db: DbDep
) -> dict[str, bool]:
    """Включает/выключает разговорный режим ИИ для конкретной группы.

    Технически — привязка/отвязка чата к единому правилу «Группа — общение ИИ»
    (rule_chats). Правило само включается, когда есть хоть один чат, и гаснет,
    когда последний убрали, — чтобы пустой список не означал «во всех чатах».
    """
    chat = await db.get(Chat, chat_id)
    if chat is None:
        raise NotFoundError("Чат не найден")
    if chat.type not in _GROUP_TYPES:
        raise InvalidInputError("Режим общения ИИ доступен только для групп")

    rule = await _get_group_chat_rule(db, create=payload.enabled)
    if rule is None:
        # Выключение, а правила ещё нет — уже выключено.
        return {"ai_chat": False}

    link_exists = (
        await db.scalar(
            select(RuleChat).where(RuleChat.rule_id == rule.id, RuleChat.chat_id == chat.id)
        )
    ) is not None

    if payload.enabled and not link_exists:
        db.add(RuleChat(rule_id=rule.id, chat_id=chat.id))
        # Групповые сообщения обрабатываются только в отслеживаемых чатах.
        chat.monitored = True
    elif not payload.enabled and link_exists:
        await db.execute(
            sa_delete(RuleChat).where(RuleChat.rule_id == rule.id, RuleChat.chat_id == chat.id)
        )

    await db.flush()
    remaining = await db.scalar(
        select(func.count()).select_from(RuleChat).where(RuleChat.rule_id == rule.id)
    )
    # Правило активно, только пока к нему привязан хотя бы один чат.
    rule.enabled = bool(remaining)
    await db.flush()
    return {"ai_chat": payload.enabled}


@leads_router.get("", response_model=list[LeadOut], summary="Лиды")
async def list_leads(
    _user: CurrentUser,
    db: DbDep,
    account_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[LeadOut]:
    stmt = select(Lead).order_by(Lead.score.desc(), Lead.last_seen_at.desc().nullslast())
    if account_id is not None:
        stmt = stmt.where(Lead.account_id == account_id)
    rows = await db.scalars(stmt.limit(limit))
    return [LeadOut.model_validate(row) for row in rows.all()]


@handoff_router.get("", response_model=list[EscalationOut], summary="Требует внимания")
async def list_escalations(
    _user: CurrentUser,
    db: DbDep,
    account_id: uuid.UUID | None = Query(default=None),
) -> list[EscalationOut]:
    """Открытые эскалации на оператора (ТЗ §21).

    Источник истины — Conversation.status: он и переводит диалог обратно в
    ACTIVE при следующем входящем сообщении, и решении оператора. Причину и
    подготовленный ответ несёт Notification — но это только контекст, не
    основа списка, поэтому джойн делаем в Python, а не в JSONB-SQL.
    """
    stmt = (
        select(
            Conversation,
            Chat.title.label("chat_title"),
            Chat.username.label("chat_username"),
            (Chat.avatar.is_not(None)).label("chat_has_avatar"),
            Account.label.label("account_label"),
        )
        .select_from(Conversation)
        .outerjoin(Chat, Chat.id == Conversation.chat_id)
        .join(Account, Account.id == Conversation.account_id)
        .where(Conversation.status == ConversationStatus.HUMAN_REQUIRED)
        .order_by(Conversation.last_message_at.desc().nullslast())
    )
    if account_id is not None:
        stmt = stmt.where(Conversation.account_id == account_id)
    rows = (await db.execute(stmt)).all()
    if not rows:
        return []

    conversation_ids = {str(row.Conversation.id) for row in rows}
    notif_rows = await db.scalars(
        select(Notification)
        .where(Notification.type == NotificationType.HUMAN_HANDOFF)
        .order_by(Notification.created_at.desc())
        .limit(500)
    )
    latest_notification: dict[str, Notification] = {}
    for notification in notif_rows.all():
        conv_id = (notification.payload or {}).get("conversation_id")
        if conv_id in conversation_ids and conv_id not in latest_notification:
            latest_notification[conv_id] = notification

    items: list[EscalationOut] = []
    for row in rows:
        conversation = row.Conversation
        matched = latest_notification.get(str(conversation.id))
        payload = matched.payload if matched and matched.payload else {}
        items.append(
            EscalationOut(
                conversation_id=conversation.id,
                account_id=conversation.account_id,
                account_label=row.account_label,
                peer_tg_id=conversation.peer_tg_id,
                chat_id=conversation.chat_id,
                chat_title=row.chat_title,
                chat_username=row.chat_username,
                chat_has_avatar=bool(row.chat_has_avatar),
                status=conversation.status,
                reason=matched.body if matched else None,
                suggested_reply=payload.get("suggested_reply"),
                notification_id=matched.id if matched else None,
                summary=conversation.summary,
                last_message_at=conversation.last_message_at,
                created_at=matched.created_at if matched else conversation.updated_at,
            )
        )
    return items


@handoff_router.post("/{conversation_id}/resolve", response_model=Ok, summary="Отметить решённым")
async def resolve_escalation(
    conversation_id: uuid.UUID,
    _user: OperatorUser,
    db: DbDep,
    redis: RedisDep,
) -> Ok:
    """Оператор ответил собеседнику сам (в Telegram) — диалог больше не ждёт.

    Реальный ответ уходит из самого Telegram силами оператора: панель здесь
    только снимает флаг ожидания, не отправляет ничего сама (ТЗ §21).
    """
    conversation = await db.get(Conversation, conversation_id)
    if conversation is None:
        raise NotFoundError("Диалог не найден")

    await ConversationRepository(db).set_status(conversation_id, ConversationStatus.ACTIVE)
    await db.execute(
        sa_update(Notification)
        .where(
            Notification.type == NotificationType.HUMAN_HANDOFF,
            Notification.read_at.is_(None),
            Notification.payload["conversation_id"].astext == str(conversation_id),
        )
        .values(read_at=utcnow())
    )
    await EventLogRepository(db).add(
        EventType.HUMAN_HANDOFF,
        level="INFO",
        account_id=conversation.account_id,
        chat_id=conversation.chat_id,
        status="human_handoff_resolved",
    )
    await db.commit()

    await EventPublisher(redis).publish(
        Event(
            type=BusEventType.HUMAN_HANDOFF,
            account_id=conversation.account_id,
            payload={"conversation_id": str(conversation_id), "resolved": True},
        )
    )
    return Ok(detail="Диалог возвращён в работу")
