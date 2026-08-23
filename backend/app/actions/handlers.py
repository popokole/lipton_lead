"""Обработчики действий (ТЗ §18).

Каждый обработчик делает одну вещь и ничего не знает про остальные. Ответ в
чат после отправки обязательно записывается в базу с признаком `is_bot_reply`:
без этого наш собственный ответ вернулся бы в конвейер как обычное сообщение
чата и запустил новый круг генерации.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.actions.engine import ActionRequest, ActionResult
from app.bus.events import EventPublisher
from app.bus.messages import Event, EventType
from app.core.clock import utcnow
from app.core.errors import TelegramError, TelegramFloodWaitError
from app.core.logging import get_logger
from app.database.repositories.chats import ChatRepository
from app.database.repositories.conversations import ConversationRepository, LeadRepository
from app.database.repositories.events import EventLogRepository
from app.database.session import Database
from app.models import (
    ActionStatus,
    ChatType,
    ConversationStatus,
    Notification,
    NotificationType,
    ProcessedStatus,
)
from app.models import EventType as LogEventType
from app.notifications.notifier import NotifierBot, format_lead_card
from app.telegram.client_manager import ClientManager
from app.telegram.peers import PeerCache
from app.telegram.sender import MessageSender

logger = get_logger(__name__)


@dataclass
class ReplyHandler:
    """Отправляет ответ в Telegram и фиксирует его в базе."""

    database: Database
    clients: ClientManager
    sender: MessageSender
    publisher: EventPublisher
    # Пропуски в чаты, собранные из входящих событий: сессия из tdata приходит
    # без кеша сущностей, и отправить по одному id Telethon не сможет.
    peers: PeerCache
    # Бот-уведомления: шлёт карточку лида в топик сценария. Опционально —
    # система работает и без отчётного бота.
    notifier: NotifierBot | None = None

    async def execute(self, request: ActionRequest, action_id: uuid.UUID) -> ActionResult:
        if not request.reply_text or not request.reply_text.strip():
            return ActionResult(status=ActionStatus.REJECTED, detail="ответ пустой")
        if request.message is None:
            return ActionResult(status=ActionStatus.FAILED, detail="нет исходного сообщения")

        client = self.clients.get(request.account_id)
        if client is None:
            return ActionResult(
                status=ActionStatus.FAILED, detail="аккаунт не подключён на этом воркере"
            )

        # Режим «в чат + в личку»: в группу уходит короткая фраза, а полноценный
        # ответ ИИ — в личку автору. dm_text и признак приходят в payload.
        dm_text = request.payload.get("dm_text")
        group_text = request.reply_text

        try:
            sent = await self.sender.send(
                request.account_id,
                client,
                chat_id=request.message.tg_chat_id,
                text=group_text,
                reply_to=request.message.tg_message_id,
                peer=self.peers.get(request.account_id, request.message.tg_chat_id),
            )
        except TelegramFloodWaitError as exc:
            return ActionResult(
                status=ActionStatus.FAILED, detail=f"Telegram просит подождать {exc.seconds}с"
            )
        except TelegramError as exc:
            return ActionResult(status=ActionStatus.FAILED, detail=exc.message)

        # Личка — «мягкое» действие: её сбой не отменяет ответ в группе,
        # который уже ушёл. Пишем предупреждение и продолжаем.
        dm_message_id: int | None = None
        if dm_text and request.message.sender_tg_id is not None:
            sender_peer = self.peers.get_sender(
                request.account_id, request.message.sender_tg_id
            )
            try:
                dm_sent = await self.sender.send(
                    request.account_id,
                    client,
                    chat_id=request.message.sender_tg_id,
                    text=str(dm_text),
                    peer=sender_peer,
                )
                dm_message_id = dm_sent.tg_message_id
            except (TelegramFloodWaitError, TelegramError) as exc:
                detail = getattr(exc, "message", str(exc))
                logger.warning(
                    "dm_reply_failed",
                    account_id=str(request.account_id),
                    peer=request.message.sender_tg_id,
                    detail=detail,
                )

        await self._record_reply(request, sent.tg_message_id, dm_message_id=dm_message_id)
        await self.publisher.publish(
            Event(
                type=EventType.ACTION_SENT,
                account_id=request.account_id,
                payload={
                    "action_id": str(action_id),
                    "tg_chat_id": request.message.tg_chat_id,
                    "tg_message_id": sent.tg_message_id,
                    "text": request.reply_text,
                },
            )
        )
        logger.info(
            "reply_sent",
            account_id=str(request.account_id),
            chat_id=request.message.tg_chat_id,
            tg_message_id=sent.tg_message_id,
        )
        return ActionResult(status=ActionStatus.SENT, sent_tg_message_id=sent.tg_message_id)

    async def _record_reply(
        self, request: ActionRequest, tg_message_id: int, *, dm_message_id: int | None = None
    ) -> None:
        from app.models import Message

        assert request.message is not None
        async with self.database.session() as db:
            db.add(
                Message(
                    account_id=request.account_id,
                    chat_id=request.chat_id,
                    conversation_id=request.conversation_id,
                    tg_chat_id=request.message.tg_chat_id,
                    tg_message_id=tg_message_id,
                    text=request.reply_text,
                    date=utcnow(),
                    reply_to_tg_message_id=request.message.tg_message_id,
                    is_incoming=False,
                    is_outgoing=True,
                    # Признак, по которому конвейер узнаёт свой собственный ответ.
                    is_bot_reply=True,
                    processed_status=ProcessedStatus.REPLIED,
                    rule_id=request.rule_id,
                )
            )
            # Ответ в личку (режим «в чат + в личку») уходит в ДРУГОЙ чат —
            # диалог с автором, а не в группу. Заводим/находим личный чат и
            # привязываем к нему запись: иначе ответ не виден в «Общении» (там
            # тред строится по chat_id) и не попадает в историю диалога.
            # tg_chat_id личного диалога равен tg_id собеседника.
            dm_text = request.payload.get("dm_text")
            if dm_message_id is not None and dm_text and request.message.sender_tg_id is not None:
                dm_chat = await ChatRepository(db).ensure(
                    request.account_id,
                    request.message.sender_tg_id,
                    chat_type=ChatType.PRIVATE,
                    title=request.message.sender_display_name
                    or (
                        f"@{request.message.sender_username}"
                        if request.message.sender_username
                        else None
                    ),
                    username=request.message.sender_username,
                )
                db.add(
                    Message(
                        account_id=request.account_id,
                        chat_id=dm_chat.id,
                        conversation_id=request.conversation_id,
                        tg_chat_id=request.message.sender_tg_id,
                        tg_message_id=dm_message_id,
                        text=str(dm_text),
                        date=utcnow(),
                        is_incoming=False,
                        is_outgoing=True,
                        is_bot_reply=True,
                        processed_status=ProcessedStatus.REPLIED,
                        rule_id=request.rule_id,
                    )
                )
                await ChatRepository(db).touch(dm_chat.id)
            if request.conversation_id is not None:
                await ConversationRepository(db).register_reply(request.conversation_id)
            await EventLogRepository(db).add(
                LogEventType.ACTION_SENT,
                account_id=request.account_id,
                chat_id=request.chat_id,
                message_id=request.message_id,
                rule_id=request.rule_id,
                scenario_id=request.scenario_id,
                status="SENT",
            )

            # Ответили человеку — значит это лид. Заводим/обновляем его прямо
            # здесь: балл пришёл из уверенности ИИ через payload действия.
            if request.message is not None and request.message.sender_tg_id is not None:
                lead = await LeadRepository(db).upsert(
                    request.account_id,
                    request.message.sender_tg_id,
                    score=int(request.payload.get("lead_score") or 40),
                    intent=request.payload.get("intent"),
                    username=request.message.sender_username,
                    display_name=request.message.sender_display_name,
                    source_chat_id=request.chat_id,
                    conversation_id=request.conversation_id,
                    scenario_id=request.scenario_id,
                )
                await EventLogRepository(db).add(
                    LogEventType.LEAD_UPDATED,
                    account_id=request.account_id,
                    chat_id=request.chat_id,
                    scenario_id=request.scenario_id,
                    status=lead.status.value,
                    extra={"score": lead.score, "tg_user_id": request.message.sender_tg_id},
                )
                await self.publisher.publish(
                    Event(
                        type=EventType.LEAD_UPDATED,
                        account_id=request.account_id,
                        payload={"score": lead.score, "status": lead.status.value},
                    )
                )

                if self.notifier is not None:
                    from app.models import Scenario as _Scenario

                    scenario_name = None
                    if request.scenario_id is not None:
                        scen = await db.get(_Scenario, request.scenario_id)
                        scenario_name = scen.name if scen else None
                    card = format_lead_card(
                        scenario_name=scenario_name,
                        account_label=str(request.account_id)[:8],
                        chat_title=request.message.chat_title,
                        sender_name=request.message.sender_display_name,
                        sender_username=request.message.sender_username,
                        sender_tg_id=request.message.sender_tg_id,
                        incoming_text=request.message.text or "",
                        reply_text=str(request.payload.get("dm_text") or request.reply_text or ""),
                        score=lead.score,
                        status=lead.status.value,
                    )
                    await self.notifier.notify_lead(
                        db,
                        scenario_id=request.scenario_id,
                        scenario_name=scenario_name,
                        text=card,
                    )
                    # Две общие ленты: все ответы в личке и все в группах.
                    # Классифицируем по источнику — где пришло сообщение.
                    await self.notifier.notify_stream(
                        db,
                        is_private=request.message.is_private,
                        text=card,
                    )


@dataclass
class ReviewHandler:
    """Кладёт сомнительный ответ на подтверждение оператору (кнопки в лог-чате)."""

    database: Database
    publisher: EventPublisher
    notifier: NotifierBot | None = None

    async def execute(self, request: ActionRequest, action_id: uuid.UUID) -> ActionResult:
        if not request.reply_text or request.message is None:
            return ActionResult(status=ActionStatus.REJECTED, detail="нечего подтверждать")
        from app.models import PendingReview

        async with self.database.session() as db:
            confidence = request.payload.get("confidence")
            review = PendingReview(
                account_id=request.account_id,
                scenario_id=request.scenario_id,
                rule_id=request.rule_id,
                chat_id=request.chat_id,
                tg_chat_id=request.message.tg_chat_id,
                reply_to_tg_message_id=request.message.tg_message_id,
                target_sender_tg_id=request.message.sender_tg_id,
                reply_text=request.reply_text,
                dm_text=request.payload.get("dm_text"),
                incoming_text=request.message.text,
                sender_username=request.message.sender_username,
                sender_display_name=request.message.sender_display_name,
                confidence=float(confidence) if confidence is not None else None,
                status="pending",
            )
            db.add(review)
            await db.flush()
            if self.notifier is not None:
                await self.notifier.send_review(db, review)
            await EventLogRepository(db).add(
                LogEventType.HUMAN_HANDOFF,
                account_id=request.account_id,
                chat_id=request.chat_id,
                message_id=request.message_id,
                rule_id=request.rule_id,
                scenario_id=request.scenario_id,
                status="REVIEW",
                extra={"confidence": confidence},
            )
        return ActionResult(status=ActionStatus.SENT, detail="на подтверждении")


@dataclass
class NotifyAdminHandler:
    """Кладёт уведомление операторам панели."""

    database: Database
    publisher: EventPublisher

    async def execute(self, request: ActionRequest, action_id: uuid.UUID) -> ActionResult:
        title = str(request.payload.get("title") or "Сработало правило")
        body = request.reply_text or str(request.payload.get("body") or "")

        async with self.database.session() as db:
            db.add(
                Notification(
                    user_id=None,
                    type=NotificationType.HUMAN_HANDOFF
                    if request.payload.get("handoff")
                    else NotificationType.NEW_LEAD,
                    title=title[:200],
                    body=body or None,
                    payload={
                        "account_id": str(request.account_id),
                        "action_id": str(action_id),
                        "rule_id": str(request.rule_id) if request.rule_id else None,
                    },
                )
            )
        await self.publisher.publish(
            Event(
                type=EventType.ACTION_CREATED,
                account_id=request.account_id,
                payload={"action_id": str(action_id), "title": title},
            )
        )
        return ActionResult(status=ActionStatus.SENT)


@dataclass
class SaveLeadHandler:
    """Заводит или обновляет лида (ТЗ §23)."""

    database: Database
    publisher: EventPublisher
    cold_below: int = 30
    hot_from: int = 70

    async def execute(self, request: ActionRequest, action_id: uuid.UUID) -> ActionResult:
        if request.message is None or request.message.sender_tg_id is None:
            return ActionResult(status=ActionStatus.REJECTED, detail="неизвестен автор сообщения")

        score = int(request.payload.get("score") or 0)
        async with self.database.session() as db:
            lead = await LeadRepository(db).upsert(
                request.account_id,
                request.message.sender_tg_id,
                score=score,
                intent=request.payload.get("intent"),
                username=request.message.sender_username,
                display_name=request.message.sender_display_name,
                source_chat_id=request.chat_id,
                conversation_id=request.conversation_id,
                scenario_id=request.scenario_id,
                cold_below=self.cold_below,
                hot_from=self.hot_from,
            )
            lead_status = lead.status.value
            lead_score = lead.score

        await self.publisher.publish(
            Event(
                type=EventType.LEAD_UPDATED,
                account_id=request.account_id,
                payload={"score": lead_score, "status": lead_status},
            )
        )
        return ActionResult(status=ActionStatus.SENT, detail=f"{lead_status} ({lead_score})")


@dataclass
class EscalateToHumanHandler:
    """Передаёт диалог человеку (ТЗ §21).

    Ничего не отправляет собеседнику: смысл передачи в том, что дальше говорит
    оператор. Подготовленный ответ сохраняется в действии, чтобы его можно было
    одобрить одной кнопкой.
    """

    database: Database
    publisher: EventPublisher

    async def execute(self, request: ActionRequest, action_id: uuid.UUID) -> ActionResult:
        reason = str(request.payload.get("reason") or "нужен оператор")

        async with self.database.session() as db:
            if request.conversation_id is not None:
                await ConversationRepository(db).set_status(
                    request.conversation_id, ConversationStatus.HUMAN_REQUIRED
                )
            db.add(
                Notification(
                    user_id=None,
                    type=NotificationType.HUMAN_HANDOFF,
                    title="Требует внимания",
                    body=reason,
                    payload={
                        "account_id": str(request.account_id),
                        "action_id": str(action_id),
                        "conversation_id": str(request.conversation_id)
                        if request.conversation_id
                        else None,
                        "suggested_reply": request.reply_text,
                    },
                )
            )
            await EventLogRepository(db).add(
                LogEventType.HUMAN_HANDOFF,
                level="WARNING",
                account_id=request.account_id,
                chat_id=request.chat_id,
                message_id=request.message_id,
                rule_id=request.rule_id,
                scenario_id=request.scenario_id,
                # Причина — свободный текст, ей место в error, а не в коротком
                # коде status. В status кладём машинную метку события.
                status="human_handoff",
                error=reason,
            )

        await self.publisher.publish(
            Event(
                type=EventType.HUMAN_HANDOFF,
                account_id=request.account_id,
                payload={
                    "action_id": str(action_id),
                    "reason": reason,
                    "suggested_reply": request.reply_text,
                },
            )
        )
        logger.info("human_handoff", account_id=str(request.account_id), reason=reason)
        return ActionResult(status=ActionStatus.SENT, detail=reason)


@dataclass
class TagUserHandler:
    """Записывает факт о собеседнике в память (ТЗ §15)."""

    database: Database

    async def execute(self, request: ActionRequest, action_id: uuid.UUID) -> ActionResult:
        from app.database.repositories.conversations import UserMemoryRepository

        if request.message is None or request.message.sender_tg_id is None:
            return ActionResult(status=ActionStatus.REJECTED, detail="неизвестен автор сообщения")

        key = str(request.payload.get("key") or "").strip()
        value = str(request.payload.get("value") or "").strip()
        if not key or not value:
            return ActionResult(status=ActionStatus.REJECTED, detail="нужны key и value")

        async with self.database.session() as db:
            await UserMemoryRepository(db).remember(
                request.account_id,
                request.message.sender_tg_id,
                key=key,
                value=value,
                source_message_id=request.message_id,
            )
        return ActionResult(status=ActionStatus.SENT, detail=f"{key}={value}")


@dataclass
class IgnoreHandler:
    """Явное «ничего не делать».

    Нужно, чтобы решение «не отвечать» тоже оставляло след в журнале: иначе
    в панели молчание системы выглядит как сбой.
    """

    database: Database

    async def execute(self, request: ActionRequest, action_id: uuid.UUID) -> ActionResult:
        async with self.database.session() as db:
            await EventLogRepository(db).add(
                LogEventType.MESSAGE_SKIPPED,
                account_id=request.account_id,
                chat_id=request.chat_id,
                message_id=request.message_id,
                rule_id=request.rule_id,
                status=str(request.payload.get("reason") or "ignored"),
            )
        return ActionResult(status=ActionStatus.SENT, detail="ignored")
