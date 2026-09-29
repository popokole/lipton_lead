"""От совпавшего правила до отправленного ответа (ТЗ §6, §45).

Порядок стадий — не стилистика, а деньги и безопасность:

    cooldown (дешёвая проверка)
      → AI-анализ
      → контекст
      → генерация
      → валидация
      → cooldown (атомарный захват)
      → действие

Первая проверка cooldown стоит ДО обращения к модели: генерировать ответ,
который всё равно не будет отправлен, — потраченные зря деньги. Вторая, уже
атомарная, стоит ПЕРЕД отправкой: между проверкой и отправкой проходит время
генерации, и за эти секунды может прийти второе сообщение того же человека.

Любой отказ на любой стадии заканчивается действием — IGNORE или
ESCALATE_TO_HUMAN. Молча ничего не делать нельзя: в панели это выглядит как
сбой, и оператор не понимает, почему система промолчала.

Своя задержка сценария (reply_delay_*) выдерживается ПОСЛЕ атомарного захвата
cooldown и ДО первой отправки — в фоновой задаче (см. _schedule), а не прямо
в handle(): иначе пауза до часа держала бы вызывающего. Всё, что столбится до
паузы (кулдаун, анти-бан лимит чата), удлиняется на её длину: отсчёт идёт от
реальной отправки, а не от получения сообщения. После паузы ответ
перепроверяется (см. _revalidate): оператор мог за это время всё отменить.

Карточка совпадения в лог-чате (MatchCard, см. app/notifications/match_log.py)
приходит в handle() от MonitorPipeline. Итог, который вернул handle(), в неё
дописывает MonitorPipeline; итог отложенного ответа — фоновая задача, во всех
своих концовках (отправлено, не отправлено, отменено, упало).
"""

from __future__ import annotations

import asyncio
import dataclasses
import math
import random
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import Select, and_, or_, select

from app.actions.cooldown import CooldownClaim, CooldownGuard, CooldownKeys
from app.actions.engine import ActionEngine, ActionRequest, ActionResult
from app.actions.validator import ReplyValidator, ValidationContext, ValidationVerdict
from app.ai.analyzer import AIAnalyzer, AnalysisOutcome
from app.ai.generator import AIGenerator, ScenarioSettings
from app.conversations.context import ContextBuilder, PromptContext
from app.core.clock import utcnow
from app.core.config import Settings
from app.core.errors import AIError
from app.core.logging import get_logger
from app.database.repositories.messages import MessageRepository
from app.database.repositories.rules import RuleRepository
from app.database.session import Database
from app.models import (
    Action,
    ActionStatus,
    ActionType,
    Chat,
    Conversation,
    ConversationStatus,
    Message,
    ProcessedStatus,
    Rule,
    Scenario,
)
from app.notifications.match_log import failed_line, skipped_line, unknown_line
from app.rules.engine import CooldownSpec, RuleMatch
from app.rules.filters import StopGuard
from app.telegram.messages import NormalizedMessage

if TYPE_CHECKING:
    from app.notifications.match_log import MatchCard

logger = get_logger(__name__)

#: Сколько при остановке воркера ждём, пока отменённые отложенные ответы
#: приберут за собой (кулдаун, эскалация, статус сообщения). Заметно меньше
#: stop_grace_period воркера в docker-compose: после уборки ещё гасятся клиенты
#: и снимаются аренды.
DELAYED_SHUTDOWN_TIMEOUT_SECONDS = 5.0
#: Запас TTL метки «ответ уже запланирован» (one_shot) сверх самой паузы — на
#: отправку с имитацией набора. Метка снимается задачей сама; TTL — страховка
#: на случай, если воркер умер, не успев прибрать.
_PENDING_MARGIN_SECONDS = 600
#: Запас, на который кулдаун и анти-бан лимит чата удлиняются сверх самой
#: паузы: отправка после неё тоже идёт не мгновенно (очередь аккаунта в
#: MessageSender, имитация набора, личка следом).
_DELAYED_SEND_MARGIN_SECONDS = 60
#: Потолок своей задержки сценария — тот же, что REPLY_DELAY_MAX_SECONDS в
#: схемах API и CHECK reply_delay_range в базе.
_REPLY_DELAY_CEILING_SECONDS = 3600
#: Метка в причине статуса отложенного ответа: по ней находим «зависшие».
SCHEDULED_REASON_MARK = "(задержка сценария)"
#: Отложенный ответ считается зависшим (воркер умер жёстко — SIGKILL, OOM,
#: перезагрузка хоста), если сообщение старше потолка паузы с большим запасом
#: на анализ, генерацию и саму отправку: живая задача так долго не ждёт.
STALE_SCHEDULED_AFTER_SECONDS = _REPLY_DELAY_CEILING_SECONDS + 900
#: Глубже не смотрим: зачистка — страховка от свежих падений, а не архивный
#: разбор, и запрос должен оставаться дешёвым.
_STALE_SCHEDULED_LOOKBACK = timedelta(days=2)
#: Диалог забрал человек: отложенный ответ, запланированный ДО этого, не шлём.
_HANDED_OVER = frozenset({ConversationStatus.HUMAN_REQUIRED, ConversationStatus.CLOSED})

Sleep = Callable[[float], Awaitable[None]]

_ACTION_TO_PROCESSED_STATUS: dict[ActionType, ProcessedStatus] = {
    ActionType.REPLY: ProcessedStatus.REPLIED,
    ActionType.IGNORE: ProcessedStatus.IGNORED,
    ActionType.ESCALATE_TO_HUMAN: ProcessedStatus.ESCALATED,
    ActionType.REQUEST_REVIEW: ProcessedStatus.ESCALATED,
}


@dataclass(frozen=True, slots=True)
class ReplyOutcome:
    """Итог обработки: что сделали и почему."""

    action: ActionType
    status: ActionStatus
    reason: str | None = None
    action_id: uuid.UUID | None = None
    analysis: AnalysisOutcome | None = None
    validation: ValidationVerdict | None = None
    # Для карточки в лог-чате (MatchLogReporter): что именно ушло.
    reply_text: str | None = None
    dm_text: str | None = None
    # Ответ в группу ушёл, а личка (режим «чат + лс») — нет.
    dm_error: str | None = None
    # Сбой самой ОТПРАВКИ (Telegram, аккаунт не подключён, падение во время
    # отправки): отличает «не удалось отправить» от «решили не отправлять».
    send_error: str | None = None
    # Отправку прервали на середине (отмена задачи): ответ мог и уйти.
    send_unknown: bool = False
    # Пауза сценария: через сколько уйдёт (PENDING) или сколько ждали (итог).
    delay_seconds: float | None = None
    # Лид после ответа / SAVE_LEAD: статус (COLD/WARM/HOT) и балл.
    lead_score: int | None = None
    lead_status: str | None = None
    # Ответ ушёл на проверку оператору: id заявки (PendingReview).
    review_id: uuid.UUID | None = None

    @property
    def replied(self) -> bool:
        return self.action is ActionType.REPLY and self.status is ActionStatus.SENT

    @property
    def scheduled(self) -> bool:
        """Ответ отложен задержкой сценария и уйдёт из фоновой задачи."""
        return self.action is ActionType.REPLY and self.status is ActionStatus.PENDING

    @property
    def processed_status(self) -> ProcessedStatus:
        """Итоговый статус сообщения-триггера по реально выполненному действию.

        ActionStatus описывает жизненный цикл самого Action (ушло/не ушло),
        а не бизнес-исход для панели — отсюда отдельный маппинг.
        """
        if self.scheduled:
            # Ещё в работе: итоговый статус запишет отложенная задача.
            return ProcessedStatus.MATCHED
        if self.status is not ActionStatus.SENT:
            return ProcessedStatus.FAILED
        return _ACTION_TO_PROCESSED_STATUS.get(self.action, ProcessedStatus.ACTED)


@dataclass(frozen=True, slots=True)
class _PreparedReply:
    """Готовый к отправке ответ: всё, что нужно, чтобы отправить его сейчас
    или после паузы сценария, и прибрать за собой, если не вышло."""

    request: ActionRequest
    claim: CooldownClaim | None
    message: NormalizedMessage
    match: RuleMatch
    chat_id: uuid.UUID | None
    message_id: uuid.UUID | None
    conversation_id: uuid.UUID | None
    analysis: AnalysisOutcome | None
    verdict: ValidationVerdict
    delay: float = 0.0
    # Метка «ответ уже запланирован» для one_shot (снимается после отправки).
    pending_key: str | None = None
    # Ключ анти-бан лимита чата, удлинённый на паузу (снимается, если ответ
    # так и не ушёл). None — лимит к этому ответу не применялся.
    chat_limit_key: str | None = None
    # Снимок состояния на момент планирования: после паузы отменяем ответ,
    # только если оператор что-то поменял ЗА ВРЕМЯ ожидания (см. _revalidate).
    scenario_enabled: bool = True
    conversation_status: ConversationStatus | None = None
    # Карточка совпадения в лог-чате: отложенная задача дописывает в неё итог
    # после паузы (без паузы итог дописывает MonitorPipeline).
    card: MatchCard | None = None


@dataclass(eq=False, slots=True)
class _DelayedJob:
    """Отложенный ответ в работе: что отправить и почему его отменили."""

    prepared: _PreparedReply
    # Причина отмены — её пишем в эскалацию (остановка воркера, отключение
    # аккаунта). Заодно признак «отмена уже запрошена»: повторный cancel()
    # прервал бы уборку самой задачи.
    cancel_reason: str | None = None
    # Задача начала выполняться. До первого шага cancel() бросил бы
    # CancelledError, не зайдя в тело корутины, — и уборки бы не было.
    started: bool = False
    # Пауза и перепроверка позади: отмена по отключению аккаунта такую задачу
    # уже не трогает — отправка сама вернёт FAILED и эскалирует.
    committed: bool = False


def reply_delay_seconds(scenario: Scenario) -> float:
    """Пауза перед авто-ответом по настройкам сценария; 0 — без паузы.

    Случайная в [min, max], чтобы ответы не приходили через одно и то же
    время. Пусто/0 в обоих полях — без паузы; задан только min — ровно min.
    """
    low = max(scenario.reply_delay_min_seconds or 0, 0)
    high = max(scenario.reply_delay_max_seconds or 0, low)
    if high <= 0:
        return 0.0
    return random.uniform(low, high)


def _extend_cooldown(spec: CooldownSpec, delay: float) -> CooldownSpec:
    """Кулдаун отложенного ответа: каждая заданная область — плюс пауза.

    Захват идёт ДО паузы, а считаться кулдаун должен от реальной отправки.
    Без удлинения ключи истекали бы, пока ответ ещё ждёт (пауза до часа при
    кулдауне по умолчанию 10 минут), и второе сообщение того же человека
    получило бы второй ответ. Нулевые области не трогаем: их не захватывают.
    """
    if delay <= 0:
        return spec
    extra = math.ceil(delay) + _DELAYED_SEND_MARGIN_SECONDS
    return dataclasses.replace(
        spec,
        **{
            field.name: getattr(spec, field.name) + extra
            for field in dataclasses.fields(spec)
            if getattr(spec, field.name) > 0
        },
    )


def stale_scheduled_query(now: datetime, *, limit: int) -> Select[tuple[Message]]:
    """Сообщения, чей отложенный ответ так и не завершился.

    Статус застрял на MATCHED «ответ через N с (задержка сценария)», сообщение
    старше потолка паузы с запасом, и по нему нет итогового действия: ни
    эскалации, ни IGNORE, ни ушедшего ответа. Так бывает, только если воркер
    умер жёстко и не успел прибрать (SIGKILL, OOM, перезагрузка хоста).
    """
    finished = (
        select(Action.id)
        .where(
            Action.message_id == Message.id,
            or_(
                Action.type.in_((ActionType.ESCALATE_TO_HUMAN, ActionType.IGNORE)),
                and_(Action.type == ActionType.REPLY, Action.status == ActionStatus.SENT),
            ),
        )
        .exists()
    )
    return (
        select(Message)
        .where(
            Message.processed_status == ProcessedStatus.MATCHED,
            Message.status_reason.contains(SCHEDULED_REASON_MARK, autoescape=True),
            Message.created_at < now - timedelta(seconds=STALE_SCHEDULED_AFTER_SECONDS),
            Message.created_at > now - _STALE_SCHEDULED_LOOKBACK,
            ~finished,
        )
        .order_by(Message.created_at)
        .limit(limit)
    )


class ReplyPipeline:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        *,
        analyzer: AIAnalyzer | None,
        generator: AIGenerator | None,
        context: ContextBuilder,
        validator: ReplyValidator,
        cooldown: CooldownGuard,
        actions: ActionEngine,
        sleep: Sleep = asyncio.sleep,
        stop_guard: StopGuard | None = None,
    ) -> None:
        self._settings = settings
        self._database = database
        self._analyzer = analyzer
        self._generator = generator
        self._context = context
        self._validator = validator
        self._cooldown = cooldown
        self._actions = actions
        self._sleep = sleep
        # Тот же стоп-лист, что у MonitorPipeline: после паузы проверяем, не
        # добавил ли оператор отправителя, пока ответ ждал.
        self._stop_guard = stop_guard
        # Отложенные ответы: держим ссылки, иначе задачу может собрать GC, и
        # отменяем их при остановке воркера (shutdown) и отключении аккаунта.
        self._delayed: dict[asyncio.Task[None], _DelayedJob] = {}
        self._closing = False

    async def shutdown(self, grace_seconds: float = DELAYED_SHUTDOWN_TIMEOUT_SECONDS) -> None:
        """Отменяет ещё не отправленные отложенные ответы и ждёт их уборки.

        Отменённая задача сама снимает кулдаун и отдаёт диалог человеку (см.
        _wait_and_deliver). Новые отложенные ответы после этого не
        планируются, а сразу уходят оператору (см. _schedule). Ждём, пока
        набор не опустеет: задача, запущенная обработчиком Telethon уже после
        первой отмены, тоже будет отменена, а не проспит до закрытия базы.
        """
        self._closing = True
        if not self._delayed:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + grace_seconds
        logger.info("delayed_replies_cancelling", count=len(self._delayed))
        while self._delayed:
            remaining = deadline - loop.time()
            if remaining <= 0:
                logger.warning("delayed_replies_cleanup_timeout", pending=len(self._delayed))
                return
            tasks = list(self._delayed)
            self._cancel_jobs(tasks, "остановка воркера")
            await asyncio.wait(tasks, timeout=remaining)

    async def cancel_account(
        self,
        account_id: uuid.UUID,
        reason: str,
        *,
        grace_seconds: float = DELAYED_SHUTDOWN_TIMEOUT_SECONDS,
    ) -> int:
        """Отменяет ждущие паузу ответы аккаунта, который ушёл с воркера.

        Иначе они досыпали бы до часа и либо разом превращались в эскалации
        «аккаунт не подключён», либо — если аккаунт успели подключить снова —
        уходили устаревшими. Отменённая задача снимает кулдаун и один раз
        отдаёт диалог оператору. Уже отправляющиеся ответы не трогаем.
        """
        tasks = [
            task
            for task, job in self._delayed.items()
            if job.prepared.message.account_id == account_id and not job.committed
        ]
        cancelled = self._cancel_jobs(tasks, reason)
        if not cancelled:
            return 0
        logger.info(
            "delayed_replies_cancelled_for_account",
            account_id=str(account_id),
            count=len(cancelled),
            reason=reason,
        )
        await asyncio.wait(cancelled, timeout=grace_seconds)
        return len(cancelled)

    def _cancel_jobs(
        self, tasks: list[asyncio.Task[None]], reason: str
    ) -> list[asyncio.Task[None]]:
        """Отменяет задачи, ещё не отменённые раньше; возвращает отменённые сейчас."""
        cancelled: list[asyncio.Task[None]] = []
        for task in tasks:
            job = self._delayed.get(task)
            if job is None or job.cancel_reason is not None:
                continue
            job.cancel_reason = reason
            # Ещё не стартовавшую задачу не отменяем: она увидит причину на
            # первом шаге и приберёт за собой сама (см. _wait_and_deliver).
            if job.started:
                task.cancel()
            cancelled.append(task)
        return cancelled

    async def handle(
        self,
        message: NormalizedMessage,
        match: RuleMatch,
        *,
        chat_id: uuid.UUID | None,
        message_id: uuid.UUID | None,
        card: MatchCard | None = None,
    ) -> ReplyOutcome:
        """Обрабатывает совпадение и возвращает итог.

        card — карточка совпадения в лог-чате. Итог, который вернул handle(),
        в неё дописывает вызывающий; итог отложенного ответа (задержка
        сценария) — фоновая задача, поэтому карточка едет в _PreparedReply.
        """
        rule = match.rule

        if rule.action is not ActionType.REPLY:
            # Уведомления, лиды и метки не требуют ни AI, ни задержек.
            return await self._simple_action(message, match, chat_id, message_id)

        cooldown_keys = CooldownKeys(
            account_id=message.account_id,
            tg_chat_id=message.tg_chat_id,
            peer_tg_id=message.sender_tg_id,
            rule_id=rule.id,
            scenario_id=rule.scenario_id,
        )

        early = await self._cooldown.check(cooldown_keys, rule.cooldown)
        if not early:
            return await self._ignore(
                message, match, chat_id, message_id, f"cooldown: {early.blocked_by}"
            )

        # Флаги чата: тест-чат снимает все ограничения (можно тестить всегда),
        # cooldown_exempt — только анти-бан лимит, reply_settings — про
        # неповторение недавних ответов (см. ниже, у генерации).
        cooldown_exempt, test_mode, reply_settings = await self._chat_flags(chat_id)

        # Анти-бан: не чаще раза в N минут в один чат (группу). Личку не трогаем
        # (там свой one_shot/логика), тест-чаты и cooldown_exempt — без лимита.
        chat_cd = self._settings.chat_reply_cooldown_seconds
        # Ключ лимита нужен и дальше: при паузе сценария его удлиняют (_send).
        chat_limit_key: str | None = None
        if (
            chat_cd > 0
            and not test_mode
            and not cooldown_exempt
            and not message.is_private
        ):
            chat_limit_key = f"chatcd:{message.account_id}:{message.tg_chat_id}"
            if not await self._cooldown.claim_once(chat_limit_key, chat_cd):
                return await self._ignore(
                    message, match, chat_id, message_id, "анти-бан: лимит на чат"
                )

        # По-человечески: ночью не отвечаем (вне рабочих часов). В тест-чате
        # отвечаем в любое время.
        if not test_mode and not self._within_work_hours():
            return await self._ignore(
                message, match, chat_id, message_id, "вне рабочих часов"
            )

        analysis: AnalysisOutcome | None = None
        review_mode = False
        if rule.ai_enabled:
            if self._analyzer is None:
                return await self._escalate(
                    message, match, chat_id, message_id, "AI не настроен, нужен оператор"
                )

            scenario = await self._load_scenario(rule.scenario_id)
            # Для анализа берём критерий лида, а не генеративную персону: персона
            # слишком лояльна и даёт ложные срабатывания (шутки, «не мне» и т.п.).
            analysis_task = (
                (scenario.lead_criteria or scenario.system_prompt) if scenario else rule.name
            )
            analysis = await self._analyzer.analyze(
                system_prompt=analysis_task,
                message_text=message.text,
                threshold=rule.ai_threshold or 0.0,
                rule_name=rule.name,
                model=scenario.model if scenario else None,
                account_id=message.account_id,
                message_id=message_id,
                scenario_id=rule.scenario_id,
            )

            # Технический сбой анализатора (провайдер недоступен, битый ответ)
            # — это не решение модели, а поломка инфраструктуры. Эскалируем
            # ВСЕГДА, независимо от human_handoff_enabled: этот флаг про то,
            # передавать ли человеку по СУЩЕСТВУ (модель не уверена/просит
            # оператора), а не про то, можно ли молча терять лида при сбое AI.
            # Иначе confidence=0.00 из _failure() неотличим от «модель
            # проверила и решила не отвечать» — лид тихо пропадает.
            if analysis.failed:
                return await self._escalate(
                    message,
                    match,
                    chat_id,
                    message_id,
                    analysis.failure_reason or "AI недоступен",
                    analysis=analysis,
                )

            # Передача человеку — по флагу сценария. Модель осторожничает и на
            # чувствительных темах (психолог, здоровье) сама поднимает
            # needs_human; для авто-ответа на лиды это выключается в сценарии.
            handoff_on = scenario.human_handoff_enabled if scenario else True

            if handoff_on and analysis.needs_human:
                # Разделяем два повода эскалации, чтобы причина не врала:
                # либо модель попросила человека, либо она не уверена.
                if analysis.result.needs_human:
                    reason = analysis.result.reason or "модель просит передать человеку"
                else:
                    reason = (
                        f"низкая уверенность AI "
                        f"({analysis.result.confidence:.2f} < {analysis.threshold:.2f})"
                    )
                return await self._escalate(
                    message, match, chat_id, message_id, reason, analysis=analysis
                )

            # analysis.failed сюда никогда не доходит — сбой уже перехвачен
            # и эскалирован выше. Здесь только настоящее решение модели:
            # не уверена или посчитала нерелевантным.
            if not analysis.passes_threshold or not analysis.result.relevant:
                conf = analysis.result.confidence
                review_min = (
                    float(scenario.review_min_confidence)
                    if scenario and scenario.review_min_confidence is not None
                    else 0.4
                )
                borderline = (
                    scenario is not None
                    and scenario.review_when_uncertain
                    and analysis.result.relevant
                    and review_min <= conf < analysis.threshold
                )
                if borderline:
                    review_mode = True
                else:
                    return await self._ignore(
                        message,
                        match,
                        chat_id,
                        message_id,
                        f"AI: не отвечать (confidence {conf:.2f}, "
                        f"порог {analysis.threshold:.2f})",
                        analysis=analysis,
                    )

        scenario = await self._load_scenario(rule.scenario_id)
        if scenario is None or self._generator is None:
            return await self._escalate(
                message, match, chat_id, message_id, "нет сценария для ответа", analysis=analysis
            )

        # «Один заход»: если с этим собеседником уже связывались — больше не
        # пишем (одно первое сообщение с контактом и всё).
        one_shot_peer = message.sender_tg_id if scenario.one_shot and not test_mode else None
        if one_shot_peer is not None:
            if await self._already_contacted(message.account_id, one_shot_peer):
                return await self._ignore(
                    message,
                    match,
                    chat_id,
                    message_id,
                    "one_shot: уже связались",
                    analysis=analysis,
                )
            # Лид заводится только на отправке, а отложенный ответ ещё ждёт
            # паузу сценария: дешёвая проверка метки до обращения к модели.
            # Атомарный захват — в _send.
            pending_key = _one_shot_pending_key(message.account_id, one_shot_peer)
            if await self._cooldown.is_held(pending_key):
                return await self._ignore(
                    message,
                    match,
                    chat_id,
                    message_id,
                    "one_shot: ответ уже запланирован",
                    analysis=analysis,
                )

        context = await self._context.build(
            account_id=message.account_id,
            tg_chat_id=message.tg_chat_id,
            peer_tg_id=message.sender_tg_id,
            current_tg_message_id=message.tg_message_id,
            context_messages=scenario.context_messages,
        )

        # A/B заходов: первый ответ новому лиду берём из варианта-захода, а не из
        # ИИ, чтобы честно сравнить конверсию. Дальше диалог ведёт ИИ.
        ab = await self._pick_ab_variant(scenario.id, message.account_id, message.sender_tg_id)
        if ab is not None and not review_mode:
            ab_id, ab_text = ab
            return await self._send(
                message,
                match,
                chat_id,
                message_id,
                context,
                scenario,
                text=ab_text,
                used_knowledge=False,
                cooldown_keys=cooldown_keys,
                one_shot_peer=one_shot_peer,
                chat_limit_key=chat_limit_key,
                analysis=analysis,
                ab_variant_id=ab_id,
                card=card,
            )

        # Провайдер может отказать: перегрузка агрегатора, таймаут, обрыв сети.
        # Исключение здесь нельзя пускать наверх — сообщение просто исчезнет из
        # виду: ни ответа, ни отметки в панели. Обрабатываем так же, как пустой
        # ответ модели: подставляем заготовленный текст или зовём человека.
        knowledge = await self._retrieve_knowledge(scenario, message.text)

        # В группах не примешиваем историю своих прошлых ответов: разным людям
        # там уместно отвечать похоже, это не выглядит подозрительно так, как в
        # личном диалоге с одним и тем же человеком. Только личка + настройка чата.
        avoid_repeat, repeat_depth = _reply_settings(reply_settings)
        recent_replies = (
            context.recent_replies[:repeat_depth]
            if avoid_repeat and message.is_private
            else ()
        )

        generation = None
        generation_error: str | None = None
        try:
            generation = await self._generator.generate(
                _scenario_settings(scenario),
                message_text=message.text,
                context=context.history,
                knowledge=knowledge,
                memory=context.memory,
                conversation_summary=context.summary,
                recent_replies=recent_replies,
                account_id=message.account_id,
                message_id=message_id,
                scenario_id=scenario.id,
            )
        except AIError as exc:
            generation_error = exc.message
            logger.warning(
                "ai_generation_failed",
                account_id=str(message.account_id),
                chat_id=message.tg_chat_id,
                detail=exc.message,
            )

        # Модель может «отказаться», но при этом дать содержательный уточняющий
        # вопрос («напишите город — подберу»). Для авто-ответа это нормальный
        # ответ: если хендофф выключен и текст осмысленный, отправляем его.
        # Но если это ОБЪЯСНЕНИЕ ОТКАЗА («не могу...»), а не вопрос собеседнику,
        # его нельзя отправлять в чат — это внутренний текст модели, а не реплика.
        if generation is not None and generation.refused and not scenario.human_handoff_enabled:
            clarification = (
                generation.reply.refusal_reason
                if generation.reply and generation.reply.refusal_reason
                else None
            )
            if (
                clarification
                and len(clarification.strip()) >= 8
                and not _looks_like_policy_refusal(clarification)
            ):
                return await self._send(
                    message,
                    match,
                    chat_id,
                    message_id,
                    context,
                    scenario,
                    text=clarification.strip(),
                    used_knowledge=False,
                    cooldown_keys=cooldown_keys,
                    one_shot_peer=one_shot_peer,
                    chat_limit_key=chat_limit_key,
                    analysis=analysis,
                    card=card,
                )

        if generation is None or not generation.has_text or generation.refused:
            reason = (
                generation_error
                or (generation.failure_reason if generation else None)
                or (
                    generation.reply.refusal_reason
                    if generation and generation.reply
                    else "модель не дала ответ"
                )
            )
            if scenario.fallback_texts:
                # Заранее написанный человеком текст лучше выдуманного ответа.
                # Пул, а не один текст: чтобы повторный сбой не звучал как бот.
                return await self._send(
                    message,
                    match,
                    chat_id,
                    message_id,
                    context,
                    scenario,
                    text=random.choice(scenario.fallback_texts),
                    used_knowledge=False,
                    cooldown_keys=cooldown_keys,
                    one_shot_peer=one_shot_peer,
                    chat_limit_key=chat_limit_key,
                    analysis=analysis,
                    card=card,
                )
            return await self._escalate(
                message, match, chat_id, message_id, reason, analysis=analysis
            )

        assert generation is not None
        assert generation.reply is not None
        return await self._send(
            message,
            match,
            chat_id,
            message_id,
            context,
            scenario,
            text=generation.reply.text,
            group_text=generation.reply.group_text,
            used_knowledge=generation.reply.used_knowledge,
            cooldown_keys=cooldown_keys,
            one_shot_peer=one_shot_peer,
            chat_limit_key=chat_limit_key,
            analysis=analysis,
            review=review_mode,
            card=card,
        )

    # --- отправка ----------------------------------------------------------
    async def _send(
        self,
        message: NormalizedMessage,
        match: RuleMatch,
        chat_id: uuid.UUID | None,
        message_id: uuid.UUID | None,
        context: PromptContext,
        scenario: Scenario,
        *,
        text: str,
        group_text: str = "",
        used_knowledge: bool,
        cooldown_keys: CooldownKeys,
        analysis: AnalysisOutcome | None,
        review: bool = False,
        ab_variant_id: uuid.UUID | None = None,
        one_shot_peer: int | None = None,
        chat_limit_key: str | None = None,
        card: MatchCard | None = None,
    ) -> ReplyOutcome:
        verdict = self._validator.validate(
            ValidationContext(
                text=text,
                used_knowledge=used_knowledge,
                require_grounding=scenario.require_knowledge_grounding,
                max_length=scenario.max_reply_length or self._settings.max_message_length,
                ai_replies_in_row=context.ai_replies_in_row,
                max_replies_in_row=self._settings.max_consecutive_ai_replies,
            )
        )
        if not verdict:
            return await self._escalate(
                message,
                match,
                chat_id,
                message_id,
                verdict.first_failure or "ответ не прошёл проверку",
                analysis=analysis,
                validation=verdict,
                conversation_id=context.conversation_id,
            )

        # Лид = тот, кому мы ответили хотя бы раз. Балл берём из уверенности
        # ИИ (0..100); без AI-проверки — базовый, чтобы лид всё равно завёлся.
        if analysis is not None:
            lead_score = round(analysis.result.confidence * 100)
            intent = analysis.result.intent.value
            confidence: float | None = analysis.result.confidence
        else:
            lead_score = 40
            intent = None
            confidence = None

        # Групповой лид при включённом reply_in_dm: в чат — короткая фраза
        # (её ИИ сгенерил в group_text; запасная — из сценария), а развёрнутый
        # ответ ИИ уходит автору в личку.
        reply_text = text
        dm_text: str | None = None
        if scenario.reply_in_dm and not message.is_private and message.sender_tg_id is not None:
            reply_text = group_text or scenario.group_ack_text or "Отправлю в лс 🙂"
            dm_text = text

        # ИИ не уверен: не отправляем сразу, а кладём на подтверждение оператору
        # (карточка с кнопками в лог-чате). Кулдаун здесь не трогаем.
        if review:
            review_payload: dict[str, object] = {"confidence": confidence}
            if dm_text:
                review_payload["dm_text"] = dm_text
            result = await self._actions.dispatch(
                ActionRequest(
                    type=ActionType.REQUEST_REVIEW,
                    account_id=message.account_id,
                    dedup_key=_dedup_key(message, ActionType.REQUEST_REVIEW),
                    message=message,
                    chat_id=chat_id,
                    message_id=message_id,
                    conversation_id=context.conversation_id,
                    rule_id=match.rule.id,
                    scenario_id=scenario.id,
                    reply_text=reply_text,
                    validation=verdict.to_payload(),
                    payload=review_payload,
                )
            )
            return ReplyOutcome(
                action=ActionType.REQUEST_REVIEW,
                status=result.status,
                reason="на подтверждении оператора",
                action_id=result.action_id,
                analysis=analysis,
                validation=verdict,
                review_id=result.review_id,
            )

        # Своя пауза сценария. «Один заход» при паузе столбим атомарно ДО
        # ожидания: лид заводится только на отправке, и без метки второе
        # сообщение того же собеседника за время паузы получило бы свой ответ.
        delay = reply_delay_seconds(scenario)
        pending_key: str | None = None
        if delay > 0 and one_shot_peer is not None:
            pending_key = _one_shot_pending_key(message.account_id, one_shot_peer)
            ttl = math.ceil(delay) + _PENDING_MARGIN_SECONDS
            if not await self._cooldown.claim_once(pending_key, ttl):
                return await self._ignore(
                    message,
                    match,
                    chat_id,
                    message_id,
                    "one_shot: ответ уже запланирован",
                    analysis=analysis,
                )

        # При паузе кулдаун удлиняется на неё: отсчёт — от реальной отправки,
        # а второе сообщение того же человека за время ожидания упрётся в него.
        allowed, claim = await self._cooldown.claim(
            cooldown_keys, _extend_cooldown(match.rule.cooldown, delay)
        )
        if not allowed:
            await self._release_pending(pending_key)
            return await self._ignore(
                message,
                match,
                chat_id,
                message_id,
                f"cooldown: {allowed.blocked_by}",
                analysis=analysis,
            )

        # Анти-бан лимит чата застолблен при получении сообщения; при паузе
        # продлеваем его до реальной отправки — иначе два отложенных ответа из
        # разных «окон» могли бы уйти в одну группу почти одновременно.
        extended_chat_limit_key: str | None = None
        if delay > 0 and chat_limit_key is not None:
            ttl = (
                self._settings.chat_reply_cooldown_seconds
                + math.ceil(delay)
                + _DELAYED_SEND_MARGIN_SECONDS
            )
            try:
                await self._cooldown.extend_once(chat_limit_key, ttl)
                extended_chat_limit_key = chat_limit_key
            except Exception as exc:  # noqa: BLE001 — лимит остаётся от получения
                logger.warning("chat_limit_extend_failed", detail=str(exc)[:150])

        # Антидубликат: не отправлять байт-в-байт повтор с этого аккаунта.
        reply_text = await self._dedupe_text(message.account_id, reply_text)
        if dm_text:
            dm_text = await self._dedupe_text(message.account_id, dm_text)

        action_payload: dict[str, object] = {"lead_score": lead_score, "intent": intent}
        if dm_text:
            action_payload["dm_text"] = dm_text
        if ab_variant_id is not None:
            action_payload["ab_variant_id"] = str(ab_variant_id)

        prepared = _PreparedReply(
            request=ActionRequest(
                type=ActionType.REPLY,
                account_id=message.account_id,
                dedup_key=_dedup_key(message, ActionType.REPLY),
                message=message,
                chat_id=chat_id,
                message_id=message_id,
                conversation_id=context.conversation_id,
                rule_id=match.rule.id,
                scenario_id=scenario.id,
                reply_text=reply_text,
                validation=verdict.to_payload(),
                payload=action_payload,
            ),
            claim=claim,
            message=message,
            match=match,
            chat_id=chat_id,
            message_id=message_id,
            conversation_id=context.conversation_id,
            analysis=analysis,
            verdict=verdict,
            delay=delay,
            pending_key=pending_key,
            chat_limit_key=extended_chat_limit_key,
            # До вставки в базу (default ещё не применён) enabled бывает None.
            scenario_enabled=scenario.enabled is not False,
            card=card,
        )
        if delay > 0:
            return await self._schedule(prepared)
        return await self._deliver(prepared)

    async def _deliver(self, prepared: _PreparedReply) -> ReplyOutcome:
        """Отправляет готовый ответ (группа, затем личка — внутри ReplyHandler)."""
        result = await self._actions.dispatch(prepared.request)

        if result.status is not ActionStatus.SENT:
            # Ответ не ушёл — задержку надо снять, иначе следующая попытка
            # окажется заблокированной несостоявшимся ответом.
            await self._cooldown.release(prepared.claim)
            # Технический сбой ОТПРАВКИ (напр. Telethon уронил RPC на битом
            # TL-объекте, см. инцидент 2026-09-04) — тот же принцип, что и для
            # analysis.failed выше: это поломка инфраструктуры, а не решение
            # модели, и лид уже отработан (анализ прошёл, ответ сгенерирован).
            # Без явной эскалации это молча оседает статусом FAILED и никто,
            # кроме самого оператора, листающего панель, об этом не узнает.
            escalated = await self._escalate(
                prepared.message,
                prepared.match,
                prepared.chat_id,
                prepared.message_id,
                f"не удалось отправить ответ: {result.detail}",
                analysis=prepared.analysis,
                validation=prepared.verdict,
                conversation_id=prepared.conversation_id,
            )
            return dataclasses.replace(
                escalated, send_error=result.detail or "неизвестная ошибка отправки"
            )

        dm_text = prepared.request.payload.get("dm_text")
        return ReplyOutcome(
            action=ActionType.REPLY,
            status=result.status,
            reason=result.detail,
            action_id=result.action_id,
            analysis=prepared.analysis,
            validation=prepared.verdict,
            reply_text=prepared.request.reply_text,
            dm_text=str(dm_text) if dm_text else None,
            dm_error=result.dm_error,
            delay_seconds=prepared.delay or None,
            lead_score=result.lead_score,
            lead_status=result.lead_status,
        )

    # --- отложенная отправка (задержка сценария) ---------------------------
    async def _schedule(self, prepared: _PreparedReply) -> ReplyOutcome:
        """Откладывает отправку на prepared.delay секунд в фоновую задачу.

        Ждать прямо здесь нельзя: handle() вызывают и обработчик Telethon, и
        реконсайлер, который идёт по сообщениям всех аккаунтов подряд одним
        циклом, — пауза до часа остановила бы довыгрузку для всех. Ожидание
        идёт ДО отправки, то есть вне блокировки аккаунта в MessageSender:
        другие ответы с этого аккаунта за это время уходят как обычно.

        Кулдаун (и метка one_shot) уже застолблены на всё время паузы, поэтому
        второе сообщение того же собеседника за это время второго ответа не
        получит.
        """
        if self._closing:
            return await self._abandon(prepared)

        reason = f"ответ через {prepared.delay:.0f} с {SCHEDULED_REASON_MARK}"
        # Статус «в работе» пишем ДО запуска задачи: иначе быстрая задача
        # могла бы записать итог раньше, чем вызывающий — промежуточный.
        # Заодно запоминаем статус диалога — для перепроверки после паузы.
        try:
            conversation_status = await self._mark_scheduled(prepared, reason)
        except Exception:
            await self._release_unsent(prepared)
            await self._release_pending(prepared.pending_key)
            raise
        prepared = dataclasses.replace(prepared, conversation_status=conversation_status)

        # Пока писали статус, могла начаться остановка воркера. Проверка и
        # регистрация задачи ниже идут без await между ними: либо shutdown()
        # увидит задачу в self._delayed, либо мы увидим _closing.
        if self._closing:
            return await self._abandon(prepared)

        message = prepared.message
        job = _DelayedJob(prepared)
        task = asyncio.create_task(
            self._deliver_later(job),
            name=f"delayed-reply-{message.account_id}-{message.tg_chat_id}-{message.tg_message_id}",
        )
        self._delayed[task] = job
        task.add_done_callback(self._on_delayed_done)
        logger.info("reply_scheduled", delay_seconds=round(prepared.delay, 1), **message.for_log())
        return ReplyOutcome(
            action=ActionType.REPLY,
            status=ActionStatus.PENDING,
            reason=reason,
            analysis=prepared.analysis,
            validation=prepared.verdict,
            delay_seconds=prepared.delay,
        )

    async def _abandon(self, prepared: _PreparedReply) -> ReplyOutcome:
        """Воркер останавливается, а пауза ещё не началась: ответа не будет."""
        await self._release_pending(prepared.pending_key)
        return await self._give_up(
            prepared,
            "воркер останавливается — отложенный ответ не отправлен",
            release_cooldown=True,
        )

    async def _mark_scheduled(
        self, prepared: _PreparedReply, reason: str
    ) -> ConversationStatus | None:
        """Пишет промежуточный статус сообщения и возвращает статус диалога."""
        async with self._database.session() as db:
            if prepared.message_id is not None:
                await MessageRepository(db).set_status(
                    prepared.message_id,
                    ProcessedStatus.MATCHED,
                    rule_id=prepared.match.rule.id,
                    reason=reason,
                )
            if prepared.conversation_id is None:
                return None
            conversation = await db.get(Conversation, prepared.conversation_id)
            return conversation.status if conversation is not None else None

    async def _deliver_later(self, job: _DelayedJob) -> None:
        job.started = True
        try:
            await self._wait_and_deliver(job)
        finally:
            await self._release_pending(job.prepared.pending_key)

    async def _wait_and_deliver(self, job: _DelayedJob) -> None:
        prepared = job.prepared
        try:
            if job.cancel_reason is not None:
                # Отменили раньше, чем задача успела стартовать.
                raise asyncio.CancelledError
            await self._sleep(prepared.delay)
            skip_reason = await self._revalidate(prepared)
        except asyncio.CancelledError:
            # Отмена до отправки (остановка воркера, отключение аккаунта):
            # ответа не было — снимаем застолбленное и отдаём диалог человеку,
            # чтобы лид не пропал молча.
            await self._finish_given_up(
                prepared,
                f"отложенный ответ отменён: {job.cancel_reason or 'задача отменена'}",
                release_cooldown=True,
            )
            raise
        except Exception as exc:
            # Не смогли перепроверить (база недоступна) — слать вслепую нельзя.
            logger.exception("delayed_reply_revalidate_failed", **prepared.message.for_log())
            await self._finish_given_up(
                prepared,
                f"отложенный ответ не перепроверен: {type(exc).__name__}: {exc}",
                release_cooldown=True,
            )
            return

        job.committed = True
        if skip_reason is not None:
            await self._finish_skipped(prepared, skip_reason)
            return

        try:
            outcome = await self._deliver(prepared)
        except asyncio.CancelledError:
            # Отправка уже шла — ответ мог уйти. Кулдаун не снимаем, чтобы не
            # ответить дважды; оператор проверит диалог. В карточке — «⚠️
            # неизвестно, ушёл ли», а не «❌ не удалось»: этого мы не знаем.
            await self._finish_given_up(
                prepared,
                "отложенный ответ прерван во время отправки "
                f"({job.cancel_reason or 'задача отменена'}) — проверьте диалог",
                release_cooldown=False,
                send="unknown",
            )
            raise
        except Exception as exc:
            logger.exception("delayed_reply_failed", **prepared.message.for_log())
            await self._finish_given_up(
                prepared,
                f"отложенный ответ упал: {type(exc).__name__}: {exc}",
                release_cooldown=False,
                send="failed",
            )
            return

        _report(prepared, outcome)
        try:
            await self._set_message_status(prepared, outcome.processed_status, outcome.reason)
        except Exception:
            logger.exception("delayed_reply_status_failed", **prepared.message.for_log())

    async def _revalidate(self, prepared: _PreparedReply) -> str | None:
        """Нужен ли ещё ответ после паузы; причина отказа или None.

        Ответ подготовлен до паузы (до часа назад), а оператор за это время
        мог его отменить: добавить отправителя в стоп-лист, выключить правило
        или сценарий, снять мониторинг с чата, забрать диалог себе. Каждая
        проверка повторяет то, что уже прошло при получении сообщения, или
        сравнивает со снимком на момент планирования — так поведение с паузой
        не строже, чем без неё.
        """
        message = prepared.message
        if self._stop_guard is not None and self._stop_guard.blocked(message):
            return "стоп-лист: отправитель добавлен во время паузы"

        async with self._database.session() as db:
            rule = await db.get(Rule, prepared.match.rule.id)
            if rule is None or not rule.enabled:
                return "правило выключено во время паузы"

            scenario_id = prepared.request.scenario_id
            if scenario_id is not None:
                scenario = await db.get(Scenario, scenario_id)
                if scenario is None:
                    return "сценарий удалён во время паузы"
                if prepared.scenario_enabled and scenario.enabled is False:
                    return "сценарий выключен во время паузы"

            # Как в MonitorPipeline: личке мониторинг не нужен, группе — да.
            if prepared.chat_id is not None and not message.is_private:
                chat = await db.get(Chat, prepared.chat_id)
                if chat is None or not chat.monitored:
                    return "мониторинг чата выключен во время паузы"

            if prepared.conversation_id is not None:
                conversation = await db.get(Conversation, prepared.conversation_id)
                if (
                    conversation is not None
                    and conversation.status in _HANDED_OVER
                    and conversation.status != prepared.conversation_status
                ):
                    return "диалог передан оператору во время паузы"
        return None

    async def _finish_skipped(self, prepared: _PreparedReply, reason: str) -> None:
        """После паузы ответ больше не нужен: снимаем застолбленное, пишем IGNORE."""
        reported = False
        try:
            await self._release_unsent(prepared)
            outcome = await self._ignore(
                prepared.message,
                prepared.match,
                prepared.chat_id,
                prepared.message_id,
                reason,
                analysis=prepared.analysis,
            )
            _report(prepared, outcome)
            reported = True
            await self._set_message_status(prepared, outcome.processed_status, reason)
        except asyncio.CancelledError:
            # Отмена посреди уборки: итог в карточку всё равно — и отмену дальше.
            if not reported:
                _report_line(prepared, skipped_line(reason))
            raise
        except Exception:
            logger.exception("delayed_reply_cleanup_failed", **prepared.message.for_log())
            if not reported:
                _report_line(prepared, skipped_line(reason))

    async def _give_up(
        self, prepared: _PreparedReply, reason: str, *, release_cooldown: bool
    ) -> ReplyOutcome:
        """Отложенный ответ не отправлен: при необходимости снимаем кулдаун и
        передаём диалог человеку — молча терять лида нельзя."""
        if release_cooldown:
            await self._release_unsent(prepared)
        logger.warning("delayed_reply_given_up", reason=reason, **prepared.message.for_log())
        return await self._escalate(
            prepared.message,
            prepared.match,
            prepared.chat_id,
            prepared.message_id,
            reason,
            analysis=prepared.analysis,
            validation=prepared.verdict,
            conversation_id=prepared.conversation_id,
        )

    async def _finish_given_up(
        self,
        prepared: _PreparedReply,
        reason: str,
        *,
        release_cooldown: bool,
        send: Literal["none", "failed", "unknown"] = "none",
    ) -> None:
        """_give_up + итоговый статус сообщения; уборка не должна падать сама.

        send — что было с отправкой: none — до неё не дошло («не
        отправляли»), failed — началась и сорвалась («не удалось
        отправить»), unknown — прервана на середине («неизвестно, ушёл ли»).
        """
        # Итог уже в карточке — упавшая следом запись статуса его не перетирает.
        reported = False
        try:
            outcome = await self._give_up(prepared, reason, release_cooldown=release_cooldown)
            if send != "none":
                outcome = dataclasses.replace(
                    outcome, send_error=reason, send_unknown=send == "unknown"
                )
            _report(prepared, outcome)
            reported = True
            await self._set_message_status(prepared, outcome.processed_status, reason)
        except asyncio.CancelledError:
            # Отмена посреди уборки: итог в карточку всё равно — и отмену дальше.
            if not reported:
                _report_line(prepared, _given_up_line(send, reason))
            raise
        except Exception:
            logger.exception("delayed_reply_cleanup_failed", **prepared.message.for_log())
            if not reported:
                _report_line(prepared, _given_up_line(send, reason))

    def _on_delayed_done(self, task: asyncio.Task[None]) -> None:
        self._delayed.pop(task, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            # Задача ловит свои ошибки сама; сюда долетает только непредвиденное.
            logger.error("delayed_reply_crashed", task=task.get_name(), exc_info=exc)

    async def _set_message_status(
        self, prepared: _PreparedReply, status: ProcessedStatus, reason: str | None
    ) -> None:
        if prepared.message_id is None:
            return
        async with self._database.session() as db:
            await MessageRepository(db).set_status(
                prepared.message_id, status, rule_id=prepared.match.rule.id, reason=reason
            )

    async def _release_unsent(self, prepared: _PreparedReply) -> None:
        """Ответ так и не ушёл: снимаем кулдаун и продлённый под паузу лимит чата.

        Лимит чата наш: его застолбили атомарно при получении сообщения, и
        пока он держался, никто другой в этот чат ответить не мог. Раз мы
        ничего не отправили, держать чат закрытым до часа незачем.
        """
        await self._cooldown.release(prepared.claim)
        if prepared.chat_limit_key is None:
            return
        try:
            await self._cooldown.release_once(prepared.chat_limit_key)
        except Exception as exc:  # noqa: BLE001 — лимит всё равно истечёт по TTL
            logger.warning("chat_limit_release_failed", detail=str(exc)[:150])

    async def _release_pending(self, key: str | None) -> None:
        if key is None:
            return
        try:
            await self._cooldown.release_once(key)
        except Exception as exc:  # noqa: BLE001 — метка всё равно истечёт по TTL
            logger.warning("one_shot_pending_release_failed", detail=str(exc)[:150])

    # --- зависшие отложенные ответы ----------------------------------------
    async def sweep_stale_scheduled(self, *, limit: int = 100) -> int:
        """Отдаёт оператору отложенные ответы, зависшие после падения воркера.

        Мягкая остановка прибирает за собой сама (shutdown). SIGKILL, OOM или
        перезагрузка хоста — нет: сообщение так и осталось бы MATCHED «ответ
        через N с», а лид пропал бы молча. Эскалация идёт через ActionEngine с
        тем же ключом идемпотентности, что у обычной, поэтому несколько
        воркеров, наткнувшихся на одну строку, не заведут двух эскалаций.
        Возвращает число переданных оператору сообщений.
        """
        live = {job.prepared.message_id for job in self._delayed.values()}
        async with self._database.session() as db:
            found = await db.scalars(stale_scheduled_query(utcnow(), limit=limit))
            rows = [row for row in found.all() if row.id not in live]
            conversation_ids: dict[uuid.UUID, uuid.UUID | None] = {}
            for row in rows:
                conversation_id = row.conversation_id
                if conversation_id is None and row.sender_tg_id is not None:
                    conversation_id = await db.scalar(
                        select(Conversation.id).where(
                            Conversation.account_id == row.account_id,
                            Conversation.peer_tg_id == row.sender_tg_id,
                        )
                    )
                conversation_ids[row.id] = conversation_id

        reason = "отложенный ответ не отправлен: воркер упал во время паузы — проверьте диалог"
        escalated = 0
        for row in rows:
            try:
                result = await self._actions.dispatch(
                    ActionRequest(
                        type=ActionType.ESCALATE_TO_HUMAN,
                        account_id=row.account_id,
                        dedup_key=_dedup_key_for(
                            ActionType.ESCALATE_TO_HUMAN,
                            row.account_id,
                            row.tg_chat_id,
                            row.tg_message_id,
                        ),
                        chat_id=row.chat_id,
                        message_id=row.id,
                        conversation_id=conversation_ids.get(row.id),
                        rule_id=row.rule_id,
                        payload={"reason": reason},
                    )
                )
                if result.status is not ActionStatus.SENT:
                    continue
                async with self._database.session() as db:
                    await MessageRepository(db).set_status(
                        row.id, ProcessedStatus.ESCALATED, reason=reason
                    )
                escalated += 1
            except Exception:
                logger.exception("stale_delayed_reply_sweep_failed", message_id=str(row.id))
        if escalated:
            logger.warning("stale_delayed_replies_escalated", count=escalated)
        return escalated

    # --- прочие действия ---------------------------------------------------
    async def _simple_action(
        self,
        message: NormalizedMessage,
        match: RuleMatch,
        chat_id: uuid.UUID | None,
        message_id: uuid.UUID | None,
    ) -> ReplyOutcome:
        payload = dict(match.rule.action_config)
        payload.setdefault("rule", match.rule.name)
        result = await self._dispatch(
            match.rule.action, message, match, chat_id, message_id, payload=payload
        )
        return ReplyOutcome(
            action=match.rule.action,
            status=result.status,
            reason=result.detail,
            action_id=result.action_id,
            lead_score=result.lead_score,
            lead_status=result.lead_status,
        )

    async def _ignore(
        self,
        message: NormalizedMessage,
        match: RuleMatch,
        chat_id: uuid.UUID | None,
        message_id: uuid.UUID | None,
        reason: str,
        *,
        analysis: AnalysisOutcome | None = None,
    ) -> ReplyOutcome:
        result = await self._dispatch(
            ActionType.IGNORE, message, match, chat_id, message_id, payload={"reason": reason}
        )
        logger.info("reply_skipped", reason=reason, **message.for_log())
        return ReplyOutcome(
            action=ActionType.IGNORE,
            status=result.status,
            reason=reason,
            action_id=result.action_id,
            analysis=analysis,
        )

    async def _escalate(
        self,
        message: NormalizedMessage,
        match: RuleMatch,
        chat_id: uuid.UUID | None,
        message_id: uuid.UUID | None,
        reason: str,
        *,
        analysis: AnalysisOutcome | None = None,
        validation: ValidationVerdict | None = None,
        conversation_id: uuid.UUID | None = None,
    ) -> ReplyOutcome:
        result = await self._dispatch(
            ActionType.ESCALATE_TO_HUMAN,
            message,
            match,
            chat_id,
            message_id,
            payload={"reason": reason},
            conversation_id=conversation_id,
            validation=validation.to_payload() if validation else None,
        )
        return ReplyOutcome(
            action=ActionType.ESCALATE_TO_HUMAN,
            status=result.status,
            reason=reason,
            action_id=result.action_id,
            analysis=analysis,
            validation=validation,
        )

    async def _dispatch(
        self,
        action: ActionType,
        message: NormalizedMessage,
        match: RuleMatch,
        chat_id: uuid.UUID | None,
        message_id: uuid.UUID | None,
        *,
        payload: dict[str, object],
        conversation_id: uuid.UUID | None = None,
        validation: dict[str, object] | None = None,
    ) -> ActionResult:
        return await self._actions.dispatch(
            ActionRequest(
                type=action,
                account_id=message.account_id,
                dedup_key=_dedup_key(message, action),
                message=message,
                chat_id=chat_id,
                message_id=message_id,
                conversation_id=conversation_id,
                rule_id=match.rule.id,
                scenario_id=match.rule.scenario_id,
                payload=payload,
                validation=validation,
            )
        )

    async def _load_scenario(self, scenario_id: uuid.UUID | None) -> Scenario | None:
        if scenario_id is None:
            return None
        async with self._database.session() as db:
            return await RuleRepository(db).get_scenario(scenario_id)

    def _within_work_hours(self) -> bool:
        """True, если сейчас рабочие часы (или режим выключен)."""
        from datetime import timedelta

        from app.core.clock import utcnow

        start, end = self._settings.work_hours_start, self._settings.work_hours_end
        if start == end:
            return True  # выключено
        hour = (utcnow() + timedelta(hours=self._settings.work_hours_tz_offset)).hour
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end  # окно через полночь

    async def _dedupe_text(self, account_id: uuid.UUID, text: str) -> str:
        """Не отправлять один и тот же текст с аккаунта дважды за окно.

        Байт-в-байт повтор — сильный признак бота. Если такой текст уже уходил
        недавно, слегка меняем хвост (смысл сохраняется), чтобы не палиться.
        """
        import hashlib

        ttl = self._settings.anti_duplicate_ttl_seconds
        if ttl <= 0 or not text.strip():
            return text
        norm = text.strip().lower()
        digest = hashlib.sha1(norm.encode()).hexdigest()[:16]
        if await self._cooldown.claim_once(f"dup:{account_id}:{digest}", ttl):
            return text
        varied = _vary_text(text)
        vdigest = hashlib.sha1(varied.strip().lower().encode()).hexdigest()[:16]
        await self._cooldown.claim_once(f"dup:{account_id}:{vdigest}", ttl)
        return varied

    async def _chat_flags(
        self, chat_id: uuid.UUID | None
    ) -> tuple[bool, bool, dict[str, Any]]:
        """(cooldown_exempt, test_mode, reply_settings) чата за один запрос."""
        if chat_id is None:
            return False, False, {}
        from sqlalchemy import select

        from app.models import Chat

        async with self._database.session() as db:
            row = (
                await db.execute(
                    select(Chat.cooldown_exempt, Chat.test_mode, Chat.reply_settings).where(
                        Chat.id == chat_id
                    )
                )
            ).first()
        if row is None:
            return False, False, {}
        return bool(row.cooldown_exempt), bool(row.test_mode), dict(row.reply_settings or {})

    async def _already_contacted(self, account_id: uuid.UUID, peer_tg_id: int) -> bool:
        """Отвечали ли этому собеседнику раньше (лид заводится на первом ответе)."""
        from sqlalchemy import select

        from app.models import Lead

        async with self._database.session() as db:
            found = await db.scalar(
                select(Lead.id).where(
                    Lead.account_id == account_id, Lead.tg_user_id == peer_tg_id
                )
            )
            return found is not None

    async def _retrieve_knowledge(self, scenario: Scenario, query: str) -> list[str]:
        """Топ релевантных кусков базы знаний сценария (пусто — если не задана)."""
        if scenario.knowledge_base_id is None:
            return []
        from app.database.repositories.knowledge import KnowledgeRepository

        try:
            async with self._database.session() as db:
                return await KnowledgeRepository(db).retrieve(
                    scenario.knowledge_base_id, query, limit=5
                )
        except Exception as exc:  # noqa: BLE001 — без базы знаний ответим и так
            logger.warning("knowledge_retrieve_failed", detail=str(exc)[:150])
            return []

    async def _pick_ab_variant(
        self, scenario_id: uuid.UUID, account_id: uuid.UUID, peer_tg_id: int | None
    ) -> tuple[uuid.UUID, str] | None:
        """Вариант-заход для ПЕРВОГО контакта, если у сценария включён A/B.

        Первый контакт = лида по этому собеседнику ещё нет (он заводится на
        первом ответе). Берём реже всего отправленный включённый вариант, чтобы
        показы распределялись ровно. Инкремент счётчика — при реальной отправке.
        """
        if peer_tg_id is None:
            return None
        from sqlalchemy import select

        from app.models import AbVariant, Lead

        async with self._database.session() as db:
            already_lead = await db.scalar(
                select(Lead.id).where(
                    Lead.account_id == account_id, Lead.tg_user_id == peer_tg_id
                )
            )
            if already_lead is not None:
                return None
            variant = await db.scalar(
                select(AbVariant)
                .where(AbVariant.scenario_id == scenario_id, AbVariant.enabled.is_(True))
                .order_by(AbVariant.sent_count.asc(), AbVariant.created_at.asc())
                .limit(1)
            )
            if variant is None:
                return None
            return variant.id, variant.text


_POLICY_REFUSAL_PREFIXES = (
    "не могу",
    "я не могу",
    "не в состоянии",
    "не буду",
    "я не буду",
    "не готова",
    "не готов",
    "sorry",
    "i cannot",
    "i can't",
    "i'm unable",
    "i am unable",
)


def _looks_like_policy_refusal(text: str) -> bool:
    """True, если это объяснение отказа модели, а не реплика собеседнику.

    Модель иногда кладёт в refusal_reason не уточняющий вопрос, а сырое
    объяснение «почему я не могу это сделать» — такой текст никогда не
    должен уйти в реальный чат.
    """
    lowered = text.strip().lower()
    return lowered.startswith(_POLICY_REFUSAL_PREFIXES)


def _reply_settings(raw: dict[str, Any]) -> tuple[bool, int]:
    """(avoid_repeat_topics, repeat_context_depth) с дефолтами и границами."""
    avoid = bool(raw.get("avoid_repeat_topics", True))
    try:
        depth = int(raw.get("repeat_context_depth", 5))
    except (TypeError, ValueError):
        depth = 5
    return avoid, max(0, min(5, depth))


_VARY_TAILS = (")", " 🙂", "", ".", " 🫶", "", " )", "..")


def _vary_text(text: str) -> str:
    """Лёгкая правка хвоста, чтобы текст не был байт-в-байт повтором."""
    from app.core.clock import utcnow

    base = text.rstrip(" .)🙂🫶")
    tail = _VARY_TAILS[utcnow().microsecond % len(_VARY_TAILS)]
    return (base + tail).strip() or text


def _scenario_settings(scenario: Scenario) -> ScenarioSettings:
    return ScenarioSettings(
        system_prompt=scenario.system_prompt,
        model=scenario.model,
        temperature=float(scenario.temperature) if scenario.temperature is not None else None,
        max_tokens=scenario.max_tokens,
        max_reply_length=scenario.max_reply_length,
        language=scenario.language,
        require_grounding=scenario.require_knowledge_grounding,
        reply_in_dm=scenario.reply_in_dm,
    )


def _report(prepared: _PreparedReply, outcome: ReplyOutcome) -> None:
    """Итог отложенного ответа — в карточку совпадения (если она есть).

    MatchCard.report синхронный и исключений не поднимает: карточка в
    лог-чате не должна ни задерживать, ни ронять ответ.
    """
    if prepared.card is not None:
        prepared.card.report(outcome)


def _report_line(prepared: _PreparedReply, line: str) -> None:
    """Запасной итог, когда ReplyOutcome получить не удалось (уборка упала).

    line — готовый HTML (skipped_line/failed_line/…: причина уже очеловечена,
    обрезана и экранирована — в ней бывает текст исключения с SQL).
    """
    if prepared.card is not None:
        prepared.card.report_line(line)


def _given_up_line(send: Literal["none", "failed", "unknown"], reason: str) -> str:
    """Запасной итог отказа от отложенного ответа, когда эскалация не удалась."""
    line = {"none": skipped_line, "failed": failed_line, "unknown": unknown_line}[send](reason)
    return f"{line} · ⚠️ передать оператору не удалось"


def _one_shot_pending_key(account_id: uuid.UUID, peer_tg_id: int) -> str:
    """Метка «этому собеседнику уже запланирован ответ» (one_shot + пауза)."""
    return f"oneshot:pending:{account_id}:{peer_tg_id}"


def _dedup_key(message: NormalizedMessage, action: ActionType) -> str:
    """Одно действие одного типа на одно сообщение — не больше."""
    return _dedup_key_for(action, *message.dedup_key)


def _dedup_key_for(
    action: ActionType, account_id: uuid.UUID, tg_chat_id: int, tg_message_id: int
) -> str:
    return f"{action.value}:{account_id}:{tg_chat_id}:{tg_message_id}"
