"""Процесс воркера.

Собирает вместе аренду аккаунтов, Telegram-клиенты и шину команд. Внутри —
независимые циклы:

* heartbeat — пульс в Redis и продление аренд;
* poll — подбор свободных аккаунтов;
* commands — исполнение команд из API;
* супервизор каждого аккаунта — внутри ClientManager.

Циклы намеренно не связаны друг с другом: заглохший опрос базы не должен
останавливать пульс, а зависшая команда — обработку сообщений. Каждый цикл сам
ловит свои ошибки и продолжает работу.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from app.actions.cooldown import CooldownGuard
from app.actions.engine import ActionEngine
from app.actions.handlers import (
    EscalateToHumanHandler,
    IgnoreHandler,
    NotifyAdminHandler,
    ReplyHandler,
    ReviewHandler,
    SaveLeadHandler,
    TagUserHandler,
)
from app.actions.validator import ReplyValidator
from app.ai.analyzer import AIAnalyzer
from app.ai.budget import AIBudget, UsageRecorder
from app.ai.generator import AIGenerator
from app.ai.registry import build_provider, provider_is_configured
from app.bus.commands import CommandConsumer
from app.bus.events import EventPublisher
from app.conversations.context import ContextBuilder
from app.core.clock import utcnow
from app.core.crypto import build_secret_box
from app.core.logging import bind_request_context, get_logger
from app.core.runtime import VERSION, Runtime
from app.database.repositories.accounts import AccountRepository
from app.database.repositories.workers import WorkerRepository
from app.models import AccountStatus, ActionType, WorkerStatus
from app.notifications.notifier import NotifierBot
from app.pipeline.monitor_pipeline import MonitorPipeline
from app.pipeline.reply_pipeline import ReplyPipeline
from app.rules.engine import RuleEngine
from app.rules.filters import SelfGuard, StopGuard
from app.telegram.account_manager import AccountManager
from app.telegram.auth_flow import AuthFlow
from app.telegram.client import TelethonClientFactory
from app.telegram.client_manager import ClientManager
from app.telegram.peers import PeerCache
from app.telegram.sender import MessageSender
from app.telegram.session_manager import SessionManager
from app.workers.command_handler import CommandHandler
from app.workers.lease import AccountLease
from app.workers.registry import (
    STATUS_ERROR,
    STATUS_HEALTHY,
    STATUS_STARTING,
    STATUS_STOPPED,
    WorkerHeartbeat,
    build_heartbeat,
)

logger = get_logger(__name__)

ACCOUNT_POLL_INTERVAL_SECONDS = 5.0


class Worker:
    def __init__(self, runtime: Runtime | None = None) -> None:
        self.runtime = runtime or Runtime()
        self.worker_id = uuid.uuid4()
        self._stop = asyncio.Event()
        self._heartbeat: WorkerHeartbeat | None = None
        self._tasks: list[asyncio.Task[None]] = []
        # Последнее, что записано в PostgreSQL: пульс идёт в Redis, а базу
        # трогаем только когда статус или состав аккаунтов реально изменились.
        self._persisted: tuple[str, int] | None = None

        settings = self.runtime.settings
        self._sessions = SessionManager(build_secret_box(settings))
        self._sender = MessageSender(settings)
        self._peers = PeerCache()
        self._notifier = NotifierBot(build_secret_box(settings), proxy=settings.ai_proxy_url)
        self._factory = TelethonClientFactory(settings)
        self._auth = AuthFlow(settings, self._factory)
        self._self_guard = SelfGuard()
        self._stop_guard = StopGuard()
        self._rules = RuleEngine(
            self.runtime.database, default_user_cooldown=settings.default_cooldown_seconds
        )
        self._pipeline: MonitorPipeline | None = None
        self._clients = ClientManager(
            settings,
            self._factory,
            on_status=self._on_account_status,
            handler_factory=self._handler_for,
        )
        self._lease: AccountLease | None = None
        self._accounts: AccountManager | None = None
        self._commands: CommandConsumer | None = None
        self._handler: CommandHandler | None = None
        self._ai_provider: Any = None

    # --- жизненный цикл ----------------------------------------------------
    async def run(self) -> None:
        settings = self.runtime.settings
        bind_request_context(worker_id=str(self.worker_id), worker_name=settings.worker_name)

        await self.runtime.startup()
        self._build_components()
        await self._register()

        self._tasks = [
            asyncio.create_task(self._heartbeat_loop(), name="worker-heartbeat"),
            asyncio.create_task(self._poll_loop(), name="worker-poll"),
            asyncio.create_task(self._command_loop(), name="worker-commands"),
            asyncio.create_task(self._review_poll_loop(), name="worker-review"),
            asyncio.create_task(self._approved_review_loop(), name="worker-review-exec"),
            asyncio.create_task(self._digest_loop(), name="worker-digest"),
        ]
        self.set_status(STATUS_HEALTHY)
        await self._sync_status_to_db()
        logger.info("worker_started", version=VERSION, pid=os.getpid())

        try:
            await self._stop.wait()
        finally:
            await self._shutdown()

    def request_stop(self) -> None:
        logger.info("worker_stop_requested")
        self._stop.set()

    def _build_components(self) -> None:
        settings = self.runtime.settings
        redis = self.runtime.redis.client

        self._lease = AccountLease(
            redis, str(self.worker_id), ttl_seconds=settings.account_lease_ttl_seconds
        )
        self._accounts = AccountManager(
            settings,
            self.runtime.database,
            self._clients,
            self._sessions,
            self._lease,
            EventPublisher(redis),
        )
        publisher = EventPublisher(redis)
        self._pipeline = MonitorPipeline(
            settings,
            self.runtime.database,
            self._rules,
            self._self_guard,
            publisher,
            reply_pipeline=self._build_reply_pipeline(publisher, redis),
            peers=self._peers,
            stop_guard=self._stop_guard,
        )
        self._commands = CommandConsumer(redis, str(self.worker_id))
        self._handler = CommandHandler(
            settings,
            self.runtime.database,
            self._accounts,
            self._clients,
            self._auth,
            self._sessions,
            self._sender,
        )

    def _build_reply_pipeline(self, publisher: EventPublisher, redis: Any) -> ReplyPipeline:
        """Собирает ветку ответов.

        Без настроенного поставщика AI анализатор и генератор отсутствуют:
        система работает как монитор — правила срабатывают, события пишутся,
        а вместо ответа диалог передаётся человеку. Падать на старте из-за
        отсутствия ключа она не должна.
        """
        settings = self.runtime.settings
        database = self.runtime.database

        analyzer: AIAnalyzer | None = None
        generator: AIGenerator | None = None
        if provider_is_configured(settings):
            provider = build_provider(settings)
            budget = AIBudget(settings, redis)
            recorder = UsageRecorder(settings)
            analyzer = AIAnalyzer(provider, budget, recorder, database)
            generator = AIGenerator(provider, budget, recorder, database)
            self._ai_provider = provider
        else:
            logger.warning("ai_provider_not_configured", provider=settings.ai_provider)

        actions = ActionEngine(database)
        actions.register(
            ActionType.REPLY,
            ReplyHandler(
                database, self._clients, self._sender, publisher, self._peers, self._notifier
            ),
        )
        actions.register(
            ActionType.REQUEST_REVIEW,
            ReviewHandler(database, publisher, notifier=self._notifier),
        )
        actions.register(ActionType.NOTIFY_ADMIN, NotifyAdminHandler(database, publisher))
        actions.register(ActionType.SAVE_LEAD, SaveLeadHandler(database, publisher))
        actions.register(ActionType.ESCALATE_TO_HUMAN, EscalateToHumanHandler(database, publisher))
        actions.register(ActionType.TAG_USER, TagUserHandler(database))
        actions.register(ActionType.IGNORE, IgnoreHandler(database))

        return ReplyPipeline(
            settings,
            database,
            analyzer=analyzer,
            generator=generator,
            context=ContextBuilder(settings, database),
            validator=ReplyValidator(),
            cooldown=CooldownGuard(redis),
            actions=actions,
        )

    async def _register(self) -> None:
        settings = self.runtime.settings
        hostname = socket.gethostname()

        async with self.runtime.database.session() as db:
            await WorkerRepository(db).register(
                self.worker_id,
                name=settings.worker_name,
                hostname=hostname,
                pid=os.getpid(),
                version=VERSION,
            )

        self._heartbeat = build_heartbeat(
            worker_id=str(self.worker_id),
            name=settings.worker_name,
            hostname=hostname,
            pid=os.getpid(),
            version=VERSION,
            started_at=utcnow(),
            status=STATUS_STARTING,
        )
        await self.runtime.worker_registry.publish(self._heartbeat)

    async def _shutdown(self) -> None:
        self.set_status(STATUS_STOPPED)

        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

        with contextlib.suppress(Exception):
            await self._notifier.close()
        await self._auth.shutdown()
        if self._ai_provider is not None:
            with contextlib.suppress(Exception):
                await self._ai_provider.close()
        if self._accounts is not None:
            await self._accounts.shutdown()
        if self._commands is not None:
            with contextlib.suppress(Exception):
                await self._commands.drop_stream()

        with contextlib.suppress(Exception):
            await self.runtime.worker_registry.unregister(self.worker_id)

        # Аккаунты больше не за нами: панель не должна показывать мёртвого
        # владельца, а другой воркер обязан суметь их подхватить.
        with contextlib.suppress(Exception):
            async with self.runtime.database.session() as db:
                await AccountRepository(db).detach_all_of_worker(self.worker_id)
                await WorkerRepository(db).set_status(self.worker_id, WorkerStatus.STOPPED)

        await self.runtime.shutdown()
        logger.info("worker_stopped")

    # --- циклы -------------------------------------------------------------
    async def _heartbeat_loop(self) -> None:
        interval = self.runtime.settings.worker_heartbeat_seconds
        while not self._stop.is_set():
            try:
                if self._accounts is not None:
                    await self._accounts.renew_leases()
                if self._heartbeat is not None:
                    self._heartbeat.updated_at = utcnow().isoformat()
                    self._heartbeat.accounts = [
                        str(account_id) for account_id in self._clients.account_ids
                    ]
                    await self.runtime.worker_registry.publish(self._heartbeat)
                await self._sync_status_to_db()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — Redis мог моргнуть
                logger.warning("heartbeat_failed", detail=str(exc))
                self.set_status(STATUS_ERROR, last_error=str(exc))

            await self._sleep(interval)

    async def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self._accounts is not None:
                    await self._accounts.poll()
                await self._refresh_own_ids()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — база могла быть недоступна
                logger.warning("account_poll_failed", detail=str(exc))

            await self._sleep(ACCOUNT_POLL_INTERVAL_SECONDS)

    async def _command_loop(self) -> None:
        assert self._commands is not None
        assert self._handler is not None

        async for command in self._commands.listen():
            if self._stop.is_set():
                break
            result = await self._handler.handle(command)
            try:
                await self._commands.reply(result)
            except Exception as exc:  # noqa: BLE001 — ответ мог не уйти
                logger.warning("command_reply_failed", command_id=str(command.id), detail=str(exc))

    def _handler_for(self, account_id: uuid.UUID) -> Callable[[Any], Awaitable[None]]:
        """Обработчик NewMessage для конкретного аккаунта.

        Ошибка обработки одного сообщения не должна убивать обработчик: иначе
        Telethon перестанет доставлять события этому аккаунту.
        """

        async def handle(event: Any) -> None:
            if self._pipeline is None:
                return
            try:
                await self._pipeline.handle_event(account_id, event)
            except Exception:
                logger.exception("message_handling_failed", account_id=str(account_id))

        return handle

    async def _review_poll_loop(self) -> None:
        """Опрашивает бота-уведомитель на нажатия кнопок под карточками ревью.

        Один опросчик на воркер: getUpdates нельзя вызывать конкурентно. Пока
        бот не настроен — просто ждём. Решения идемпотентны по review.status.
        """
        offset = 0
        while not self._stop.is_set():
            try:
                async with self.runtime.database.session() as db:
                    loaded = await self._notifier.load_token(db)
                if loaded is None:
                    await self._sleep(15)
                    continue
                token, group_id = loaded
                updates = await self._notifier.poll_updates(token, offset)
                for upd in updates:
                    offset = max(offset, int(upd.get("update_id", 0)) + 1)
                    callback = upd.get("callback_query")
                    if callback:
                        await self._handle_review_callback(token, group_id, callback)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — опрос не критичнее работы
                logger.warning("review_poll_failed", detail=str(exc)[:200])
                await self._sleep(5)

    async def _handle_review_callback(
        self, token: str, group_id: int, callback: dict[str, Any]
    ) -> None:
        from app.core.clock import utcnow
        from app.models import PendingReview

        data = str(callback.get("data") or "")
        cb_id = str(callback.get("id") or "")
        action, _, raw_id = data.partition(":")
        try:
            review_id = uuid.UUID(raw_id)
        except ValueError:
            await self._notifier.answer_callback(token, cb_id, "неизвестная кнопка")
            return

        async with self.runtime.database.session() as db:
            review = await db.get(PendingReview, review_id)
            if review is None or review.status != "pending":
                await self._notifier.answer_callback(token, cb_id, "уже обработано")
                return
            message_id = review.notify_message_id

            if action == "rv_skip":
                review.status = "ignored"
                review.decided_at = utcnow()
                await self._notifier.answer_callback(token, cb_id, "Пропущено")
                if message_id:
                    await self._notifier.finalize_review_card(
                        token, group_id, message_id, "✖️ Пропущено оператором"
                    )
                return

            ok, detail = await self._execute_review(db, review)
            if ok:
                review.status = "sent"
                review.decided_at = utcnow()
                await self._notifier.answer_callback(token, cb_id, "Отправлено ✅")
                if message_id:
                    await self._notifier.finalize_review_card(
                        token, group_id, message_id, "✅ Отправлено оператором"
                    )
            else:
                await self._notifier.answer_callback(token, cb_id, detail[:180] or "не удалось")

    async def _execute_review(self, db: Any, review: Any) -> tuple[bool, str]:
        """Отправляет подтверждённый ответ и записывает его (как обычный ответ)."""
        from app.core.clock import utcnow
        from app.database.repositories.chats import ChatRepository
        from app.database.repositories.conversations import LeadRepository
        from app.models import ChatType, Message, ProcessedStatus

        client = self._clients.get(review.account_id)
        if client is None:
            return False, "аккаунт не подключён на воркере"
        try:
            peer = self._peers.get(review.account_id, review.tg_chat_id)
            sent = await self._sender.send(
                review.account_id,
                client,
                chat_id=review.tg_chat_id,
                text=review.reply_text,
                reply_to=review.reply_to_tg_message_id,
                peer=peer,
            )
        except Exception as exc:  # noqa: BLE001 — покажем оператору причину
            return False, f"{type(exc).__name__}: {exc}"

        db.add(
            Message(
                account_id=review.account_id,
                chat_id=review.chat_id,
                tg_chat_id=review.tg_chat_id,
                tg_message_id=sent.tg_message_id,
                text=review.reply_text,
                date=utcnow(),
                reply_to_tg_message_id=review.reply_to_tg_message_id,
                is_incoming=False,
                is_outgoing=True,
                is_bot_reply=True,
                processed_status=ProcessedStatus.REPLIED,
                rule_id=review.rule_id,
            )
        )
        # Развёрнутый ответ в личку (режим чат+лс).
        if review.dm_text and review.target_sender_tg_id is not None:
            try:
                sender_peer = self._peers.get_sender(review.account_id, review.target_sender_tg_id)
                dm_sent = await self._sender.send(
                    review.account_id,
                    client,
                    chat_id=review.target_sender_tg_id,
                    text=review.dm_text,
                    peer=sender_peer,
                )
                dm_chat = await ChatRepository(db).ensure(
                    review.account_id,
                    review.target_sender_tg_id,
                    chat_type=ChatType.PRIVATE,
                    title=review.sender_display_name
                    or (f"@{review.sender_username}" if review.sender_username else None),
                    username=review.sender_username,
                )
                db.add(
                    Message(
                        account_id=review.account_id,
                        chat_id=dm_chat.id,
                        tg_chat_id=review.target_sender_tg_id,
                        tg_message_id=dm_sent.tg_message_id,
                        text=review.dm_text,
                        date=utcnow(),
                        is_incoming=False,
                        is_outgoing=True,
                        is_bot_reply=True,
                        processed_status=ProcessedStatus.REPLIED,
                        rule_id=review.rule_id,
                    )
                )
                await ChatRepository(db).touch(dm_chat.id)
            except Exception as exc:  # noqa: BLE001 — личка вторична
                logger.warning("review_dm_failed", detail=str(exc)[:150])

        if review.target_sender_tg_id is not None:
            score = int(float(review.confidence) * 100) if review.confidence is not None else 50
            await LeadRepository(db).upsert(
                review.account_id,
                review.target_sender_tg_id,
                score=score,
                intent=None,
                username=review.sender_username,
                display_name=review.sender_display_name,
                source_chat_id=review.chat_id,
                conversation_id=None,
                scenario_id=review.scenario_id,
            )
        return True, "ok"

    async def _approved_review_loop(self) -> None:
        """Отправляет заявки ревью, одобренные из панели (status=approved)."""
        while not self._stop.is_set():
            try:
                await self._process_one_approved_review()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — не роняем цикл
                logger.warning("approved_review_loop_failed", detail=str(exc)[:150])
            await self._sleep(4)

    async def _process_one_approved_review(self) -> None:
        from sqlalchemy import select

        from app.core.clock import utcnow
        from app.models import PendingReview

        async with self.runtime.database.session() as db:
            review = await db.scalar(
                select(PendingReview).where(PendingReview.status == "approved").limit(1)
            )
            if review is None:
                return
            message_id = review.notify_message_id
            ok, detail = await self._execute_review(db, review)
            review.status = "sent" if ok else "failed"
            review.decided_at = utcnow()
            if not ok:
                logger.warning("approved_review_send_failed", detail=detail)
            loaded = await self._notifier.load_token(db)
            if loaded and message_id:
                token, group_id = loaded
                note = "✅ Отправлено из панели" if ok else f"⚠️ Не удалось: {detail[:80]}"
                await self._notifier.finalize_review_card(token, group_id, message_id, note)

    async def _digest_loop(self) -> None:
        """Раз в день шлёт сводку по лидам в лог-чат (в digest_hour)."""
        while not self._stop.is_set():
            try:
                await self._maybe_send_digest()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — дайджест не критичнее работы
                logger.warning("digest_loop_failed", detail=str(exc)[:150])
            await self._sleep(300)

    async def _maybe_send_digest(self) -> None:
        from datetime import timedelta

        from app.core.clock import utcnow

        settings = self.runtime.settings
        now_local = utcnow() + timedelta(hours=settings.work_hours_tz_offset)
        if now_local.hour != settings.digest_hour:
            return
        day = now_local.strftime("%Y-%m-%d")
        redis = self.runtime.redis.client
        # Один дайджест в день: атомарный захват ключа на ~25 часов.
        if not await redis.set(f"digest:sent:{day}", "1", nx=True, ex=90000):
            return
        text = await self._build_digest(now_local)
        async with self.runtime.database.session() as db:
            await self._notifier.notify_digest(db, text)
        logger.info("digest_sent", day=day)

    async def _build_digest(self, now_local: Any) -> str:
        from datetime import timedelta

        from sqlalchemy import func, select

        from app.models import Lead, Message, PendingReview, Scenario

        offset = self.runtime.settings.work_hours_tz_offset
        midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc = midnight - timedelta(hours=offset)

        async with self.runtime.database.session() as db:
            total = await db.scalar(
                select(func.count()).select_from(Lead).where(Lead.created_at >= start_utc)
            )
            by_scn = (
                await db.execute(
                    select(Scenario.name, func.count())
                    .select_from(Lead)
                    .outerjoin(Scenario, Scenario.id == Lead.scenario_id)
                    .where(Lead.created_at >= start_utc)
                    .group_by(Scenario.name)
                    .order_by(func.count().desc())
                )
            ).all()
            replies = await db.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.is_bot_reply.is_(True), Message.created_at >= start_utc)
            )
            pending = await db.scalar(
                select(func.count())
                .select_from(PendingReview)
                .where(PendingReview.status == "pending")
            )

        lines = [
            f"📊 <b>Дайджест</b> · {now_local.strftime('%d.%m.%Y')}",
            f"🎯 Лидов за день: {int(total or 0)}",
        ]
        for name, count in by_scn:
            lines.append(f"  • {name or 'без сценария'}: {count}")
        lines.append(f"✉️ Ответов отправлено: {int(replies or 0)}")
        if pending:
            lines.append(f"🟡 Ждут подтверждения: {int(pending)}")
        return "\n".join(lines)

    async def _refresh_own_ids(self) -> None:
        """Обновляет реестр своих id (анти-самоответ) и стоп-лист."""
        try:
            async with self.runtime.database.session() as db:
                own_ids = await AccountRepository(db).own_telegram_ids()
                from sqlalchemy import select

                from app.models import StopEntry

                rows = (await db.execute(select(StopEntry.tg_user_id, StopEntry.username))).all()
        except Exception as exc:  # noqa: BLE001 — реестр обновится на следующем круге
            logger.warning("own_ids_refresh_failed", detail=str(exc))
            return
        self._self_guard.update(own_ids)
        self._stop_guard.update(
            (r.tg_user_id for r in rows if r.tg_user_id is not None),
            (r.username for r in rows if r.username),
        )

    async def _sync_status_to_db(self) -> None:
        """Переносит статус в PostgreSQL, если он изменился."""
        if self._heartbeat is None:
            return
        snapshot = (self._heartbeat.status, len(self._clients.account_ids))
        if snapshot == self._persisted:
            return

        try:
            async with self.runtime.database.session() as db:
                await WorkerRepository(db).set_status(
                    self.worker_id,
                    WorkerStatus(snapshot[0]),
                    accounts_count=snapshot[1],
                    last_error=self._heartbeat.last_error,
                )
        except Exception as exc:  # noqa: BLE001 — статус не важнее работы
            logger.warning("worker_status_persist_failed", detail=str(exc))
            return

        self._persisted = snapshot

    async def _sleep(self, seconds: float) -> None:
        """Пауза, прерываемая остановкой воркера."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    # --- статусы -----------------------------------------------------------
    def set_status(self, status: str, *, last_error: str | None = None) -> None:
        if self._heartbeat is None:
            return
        self._heartbeat.status = status
        self._heartbeat.last_error = last_error

    async def _on_account_status(
        self, account_id: uuid.UUID, status: AccountStatus, error: str | None
    ) -> None:
        if self._accounts is None:
            return
        await self._accounts.on_client_status(account_id, status, error)
