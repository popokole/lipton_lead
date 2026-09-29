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
from app.core.clock import get_clock, utcnow
from app.core.config import Settings
from app.core.logging import get_logger
from app.database.repositories.chats import ChatRepository
from app.database.repositories.events import EventLogRepository
from app.database.repositories.messages import MessageRepository
from app.database.session import Database
from app.models import EventType, ProcessedStatus, RuleScope
from app.notifications.match_log import (
    NO_REPLY_PIPELINE_LINE,
    STOPLIST_LINE,
    MatchCard,
    MatchLogReporter,
    crash_line,
)
from app.pipeline.reply_pipeline import ReplyOutcome, ReplyPipeline
from app.rules.engine import RuleEngine, RuleMatch
from app.rules.filters import SelfGuard, StopGuard
from app.telegram.messages import MessageNormalizer, NormalizedMessage
from app.telegram.peers import PeerCache, extract_input_peer, extract_input_sender

logger = get_logger(__name__)

# Догнанные при переподключении старые сообщения сохраняем, но НЕ отвечаем на
# них: иначе после долгого простоя аккаунт разом отвечает на весь бэклог и
# выглядит как спам. Свежие (в пределах окна) обрабатываем как обычно.
REPLY_MAX_AGE_SECONDS = 600


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
        stop_guard: StopGuard | None = None,
        match_log: MatchLogReporter | None = None,
    ) -> None:
        self._settings = settings
        self._database = database
        self._rules = rules
        self._self_guard = self_guard
        self._stop_guard = stop_guard or StopGuard()
        self._publisher = publisher
        # Без ReplyPipeline система работает как монитор: правила срабатывают,
        # события пишутся, но ответы не отправляются.
        self._reply = reply_pipeline
        # Карточка в лог-чат на каждое совпадение (с итогом). Опционально —
        # без бота-уведомителя конвейер работает как раньше.
        self._match_log = match_log
        self._normalizer = MessageNormalizer(settings.max_message_length)
        self.peers = peers or PeerCache()

    async def _credit_ab_reply(self, account_id: uuid.UUID, peer_tg_id: int) -> None:
        """Засчитывает ответ собеседника варианту-заходу A/B (один раз на диалог)."""
        from sqlalchemy import select

        from app.models import AbVariant, Conversation

        try:
            async with self._database.session() as db:
                conv = await db.scalar(
                    select(Conversation).where(
                        Conversation.account_id == account_id,
                        Conversation.peer_tg_id == peer_tg_id,
                    )
                )
                if conv is None or conv.ab_variant_id is None or conv.ab_reply_counted:
                    return
                variant = await db.get(AbVariant, conv.ab_variant_id)
                if variant is not None:
                    variant.reply_count += 1
                conv.ab_reply_counted = True
        except Exception as exc:  # noqa: BLE001 — учёт A/B не критичнее обработки
            logger.warning("ab_credit_failed", detail=str(exc)[:150])

    async def _log_stoplisted_match(
        self, message: NormalizedMessage, chat_id: uuid.UUID, scope: RuleScope
    ) -> None:
        """Отправитель в стоп-листе: не отвечаем, но совпадение в лог-чат шлём.

        Правила для такого сообщения прогоняются только ради карточки —
        статус сообщения, журнал и ответ остаются как раньше (SKIPPED).
        """
        if self._match_log is None:
            return
        try:
            matches = await self._rules.match_all(message, chat_id=chat_id, scope=scope)
        except Exception as exc:  # noqa: BLE001 — карточка не важнее обработки
            logger.warning("stoplisted_match_check_failed", detail=str(exc)[:150])
            return
        if not matches:
            return
        card = self._match_log.open_card(message, matches[0])
        if card is not None:
            card.report_line(STOPLIST_LINE)

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

        # A/B: собеседник написал в личку — если ему уходил вариант-заход и его
        # ответ ещё не засчитан, кредитуем конверсию этого захода.
        if message.is_private and message.is_incoming and message.sender_tg_id is not None:
            await self._credit_ab_reply(message.account_id, message.sender_tg_id)

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
        # Старьё из догона (catch_up) только сохраняем: отвечать на бэклог после
        # простоя нельзя — это выглядит как спам и грозит баном.
        age_seconds = (utcnow() - message.date).total_seconds()
        fresh = age_seconds <= REPLY_MAX_AGE_SECONDS
        # Стоп-лист: сообщение сохранено, но отвечать этому отправителю нельзя.
        blocked = self._stop_guard.blocked(message)
        # Ответы — только в личке и в отслеживаемых чатах. В остальных сообщение
        # прочитано и сохранено, но правила не запускаются.
        if fresh and not blocked and (message.is_private or chat_monitored):
            matches = await self._rules.match_all(message, chat_id=chat_id, scope=scope)
        else:
            if blocked:
                logger.info("stoplisted_sender_skipped", **message.for_log())
                if fresh and (message.is_private or chat_monitored):
                    await self._log_stoplisted_match(message, chat_id, scope)
            elif not fresh:
                logger.info(
                    "stale_message_stored_no_reply",
                    age_seconds=int(age_seconds),
                    **message.for_log(),
                )
            matches = []

        status = ProcessedStatus.MATCHED if matches else ProcessedStatus.SKIPPED
        primary = matches[0] if matches else None

        # Карточка в лог-чат — сразу при совпадении, до ответа и независимо от
        # него: итог допишется в неё ниже (или из отложенной задачи). Сюда
        # сообщение доходит один раз — claim() выше не пустит повтор, в том
        # числе от реконсайлера. open_card только ставит задание в очередь.
        card: MatchCard | None = None
        if primary is not None and self._match_log is not None:
            card = self._match_log.open_card(message, primary)

        try:
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
        except Exception as exc:
            # До ответа дело не дошло — карточка не должна висеть «в работе».
            _report_crash(card, exc)
            raise

        if primary is not None:
            logger.info(
                "rule_match",
                rule=primary.rule.name,
                rule_id=str(primary.rule.id),
                **message.for_log(),
            )

        reply_outcome: ReplyOutcome | None = None
        if primary is not None and self._reply is not None:
            try:
                reply_outcome = await self._reply.handle(
                    message, primary, chat_id=chat_id, message_id=message_id, card=card
                )
            except Exception as exc:
                _report_crash(card, exc)
                raise
            # Итог — в карточку. Для отложенного ответа это «⏳ через N с»,
            # итог после паузы допишет его фоновая задача.
            if card is not None:
                card.report(reply_outcome)
            # MATCHED — вход в конвейер, а не итог: без этого IGNORE/ESCALATE
            # оставались бы неотличимы от «ещё обрабатывается».
            status = reply_outcome.processed_status
            # Отложенный ответ (задержка сценария) пишет статус сам: и
            # промежуточный, и итоговый — после отправки из фоновой задачи.
            if not reply_outcome.scheduled:
                async with self._database.session() as db:
                    await MessageRepository(db).set_status(
                        message_id, status, rule_id=primary.rule.id, reason=reply_outcome.reason
                    )
        elif card is not None:
            card.report_line(NO_REPLY_PIPELINE_LINE)

        return PipelineOutcome(
            status=status,
            reason=None if matches else "no rule matched",
            message_id=message_id,
            matches=tuple(matches),
            reply=reply_outcome,
        )


def _report_crash(card: MatchCard | None, exc: BaseException) -> None:
    """Обработка упала до итога: в карточку — причина, ответа не было."""
    if card is not None:
        card.report_line(crash_line(exc))
