"""Конвейер обработки сообщений из отслеживаемых чатов (ТЗ §6).

Порядок стадий выбран так, чтобы дорогое выполнялось как можно позже:

    нормализация → защита от себя → застолбить → чат → фильтры → правила

«Застолбить» стоит до всякой обработки и до записи сообщения. Это
единственное место, где решается, будет ли сообщение обработано вообще:
Telethon может доставить одно и то же событие дважды (переподключение,
догрузка пропущенного), и без атомарной отметки два обработчика одновременно
дошли бы до отправки ответа.

Отметка снимается обратно только если конвейер упал до сохранения сообщения:
тогда повторная доставка получает ещё один честный шанс. Если сообщение уже
записано, повтор не нужен — состояние в базе полное.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from app.bus.events import EventPublisher
from app.bus.messages import Event
from app.bus.messages import EventType as BusEventType
from app.core.clock import get_clock
from app.core.config import Settings
from app.core.logging import get_logger
from app.database.repositories.chats import ChatRepository
from app.database.repositories.events import EventLogRepository
from app.database.repositories.messages import MessageRepository
from app.database.session import Database
from app.models import EventType, ProcessedStatus, RuleScope
from app.pipeline.reply_pipeline import ReplyOutcome, ReplyPipeline
from app.rules.engine import RuleEngine, RuleMatch
from app.rules.filters import SelfGuard
from app.telegram.messages import MessageNormalizer, NormalizedMessage
from app.telegram.peers import PeerCache, extract_input_peer, extract_input_sender

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PipelineOutcome:
    """Что произошло с сообщением. Возвращается ради тестов и журнала."""

    status: ProcessedStatus
    reason: str | None = None
    message_id: uuid.UUID | None = None
    matches: tuple[RuleMatch, ...] = ()
    duration_ms: int = 0
    reply: ReplyOutcome | None = None

    @property
    def matched(self) -> bool:
        return bool(self.matches)

    @property
    def primary_rule(self) -> RuleMatch | None:
        return self.matches[0] if self.matches else None


class MonitorPipeline:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        rules: RuleEngine,
        self_guard: SelfGuard,
        publisher: EventPublisher,
        reply_pipeline: ReplyPipeline | None = None,
        peers: PeerCache | None = None,
    ) -> None:
        self._settings = settings
        self._database = database
        self._rules = rules
        self._self_guard = self_guard
        self._publisher = publisher
        # Без ReplyPipeline система работает как монитор: правила срабатывают,
        # события пишутся, но ответы не отправляются.
        self._reply = reply_pipeline
        self._normalizer = MessageNormalizer(settings.max_message_length)
        self.peers = peers or PeerCache()

    async def handle_event(self, account_id: uuid.UUID, event: Any) -> PipelineOutcome:
        """Точка входа для обработчика Telethon."""
        started = get_clock().monotonic()
        try:
            message = self._normalizer.normalize(account_id, event)
        except ValueError as exc:
            logger.warning("message_unparsable", account_id=str(account_id), detail=str(exc))
            return PipelineOutcome(status=ProcessedStatus.SKIPPED, reason=str(exc))

        # Пропуск в этот чат берём из самого события: другого источника нет.
        self.peers.remember(account_id, message.tg_chat_id, await extract_input_peer(event))
        if message.sender_tg_id is not None:
            self.peers.remember_sender(
                account_id, message.sender_tg_id, await extract_input_sender(event)
            )

        outcome = await self._process(message)
        elapsed_ms = int((get_clock().monotonic() - started) * 1000)
        return PipelineOutcome(
            status=outcome.status,
            reason=outcome.reason,
            message_id=outcome.message_id,
            matches=outcome.matches,
            duration_ms=elapsed_ms,
            reply=outcome.reply,
        )

    async def _process(self, message: NormalizedMessage) -> PipelineOutcome:
        guard = self._self_guard.check(message)
        if not guard:
            # Не пишем в базу и не столбим: своё сообщение просто не событие.
            logger.debug("message_from_self", **message.for_log())
            return PipelineOutcome(status=ProcessedStatus.SKIPPED, reason=guard.reason)

        async with self._database.session() as db:
            messages = MessageRepository(db)
            if not await messages.claim(message):
                logger.debug("message_already_claimed", **message.for_log())
                return PipelineOutcome(status=ProcessedStatus.SKIPPED, reason="already claimed")

            chats = ChatRepository(db)
            # Название берём из самого чата (для групп) или по имени собеседника
            # (для лички), чтобы в дереве и в ленте было видно, откуда сообщение.
            chat = await chats.ensure(
                message.account_id,
                message.tg_chat_id,
                chat_type=message.chat_type,
                title=message.chat_title
                or (message.sender_display_name if message.is_private else None),
                username=message.chat_username,
                # Слежка включена сразу для любого нового чата: оператор
                # выключает её точечно в дереве, а не включает по одному.
                monitored=True,
            )

            # Читаем и сохраняем ВСЕ входящие сообщения — это основа дерева
            # чатов и статистики активности. Отвечаем при этом только там, где
            # включён мониторинг: хранение и ответ — разные права.
            row = await messages.save(message, chat_id=chat.id)
            await chats.touch(chat.id)
            await EventLogRepository(db).add(
                EventType.MESSAGE_RECEIVED,
                account_id=message.account_id,
                chat_id=chat.id,
                message_id=row.id,
                extra={"media": message.media_type.value if message.media_type else None},
            )
            message_id = row.id
            chat_id = chat.id
            chat_monitored = chat.monitored

        await self._publisher.publish(
            Event(
                type=BusEventType.MESSAGE_NEW,
                account_id=message.account_id,
                payload={
                    "message_id": str(message_id),
                    "chat_id": str(chat_id),
                    "tg_chat_id": message.tg_chat_id,
                    "sender_id": message.sender_tg_id,
                    "text": message.text,
                    "date": message.date.isoformat(),
                },
            )
        )

        scope = RuleScope.DIALOG if message.is_private else RuleScope.CHAT_MONITOR
        # Ответы — только в личке и в отслеживаемых чатах. В остальных сообщение
        # прочитано и сохранено, но правила не запускаются.
        if message.is_private or chat_monitored:
            matches = await self._rules.match_all(message, chat_id=chat_id, scope=scope)
        else:
            matches = []

        status = ProcessedStatus.MATCHED if matches else ProcessedStatus.SKIPPED
        primary = matches[0] if matches else None

        async with self._database.session() as db:
            await MessageRepository(db).set_status(
                message_id, status, rule_id=primary.rule.id if primary else None
            )
            if primary is not None:
                await EventLogRepository(db).add(
                    EventType.RULE_MATCH,
                    account_id=message.account_id,
                    chat_id=chat_id,
                    message_id=message_id,
                    rule_id=primary.rule.id,
                    scenario_id=primary.rule.scenario_id,
                    extra={
                        "rule": primary.rule.name,
                        "terms": list(primary.matched_terms),
                        "also_matched": [match.rule.name for match in matches[1:]],
                    },
                )

        if primary is not None:
            logger.info(
                "rule_match",
                rule=primary.rule.name,
                rule_id=str(primary.rule.id),
                **message.for_log(),
            )

        reply_outcome: ReplyOutcome | None = None
        if primary is not None and self._reply is not None:
            reply_outcome = await self._reply.handle(
                message, primary, chat_id=chat_id, message_id=message_id
            )
            # MATCHED — вход в конвейер, а не итог: без этого IGNORE/ESCALATE
            # оставались бы неотличимы от «ещё обрабатывается».
            status = reply_outcome.processed_status
            async with self._database.session() as db:
                await MessageRepository(db).set_status(
                    message_id, status, rule_id=primary.rule.id, reason=reply_outcome.reason
                )

        return PipelineOutcome(
            status=status,
            reason=None if matches else "no rule matched",
            message_id=message_id,
            matches=tuple(matches),
            reply=reply_outcome,
        )
