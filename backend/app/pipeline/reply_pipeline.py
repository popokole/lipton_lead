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
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.actions.cooldown import CooldownGuard, CooldownKeys
from app.actions.engine import ActionEngine, ActionRequest, ActionResult
from app.actions.validator import ReplyValidator, ValidationContext, ValidationVerdict
from app.ai.analyzer import AIAnalyzer, AnalysisOutcome
from app.ai.generator import AIGenerator, ScenarioSettings
from app.conversations.context import ContextBuilder, PromptContext
from app.core.config import Settings
from app.core.errors import AIError
from app.core.logging import get_logger
from app.database.repositories.rules import RuleRepository
from app.database.session import Database
from app.models import ActionStatus, ActionType, ProcessedStatus, Scenario
from app.rules.engine import RuleMatch
from app.telegram.messages import NormalizedMessage

logger = get_logger(__name__)

_ACTION_TO_PROCESSED_STATUS: dict[ActionType, ProcessedStatus] = {
    ActionType.REPLY: ProcessedStatus.REPLIED,
    ActionType.IGNORE: ProcessedStatus.IGNORED,
    ActionType.ESCALATE_TO_HUMAN: ProcessedStatus.ESCALATED,
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

    @property
    def replied(self) -> bool:
        return self.action is ActionType.REPLY and self.status is ActionStatus.SENT

    @property
    def processed_status(self) -> ProcessedStatus:
        """Итоговый статус сообщения-триггера по реально выполненному действию.

        ActionStatus описывает жизненный цикл самого Action (ушло/не ушло),
        а не бизнес-исход для панели — отсюда отдельный маппинг.
        """
        if self.status is not ActionStatus.SENT:
            return ProcessedStatus.FAILED
        return _ACTION_TO_PROCESSED_STATUS.get(self.action, ProcessedStatus.ACTED)


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
    ) -> None:
        self._settings = settings
        self._database = database
        self._analyzer = analyzer
        self._generator = generator
        self._context = context
        self._validator = validator
        self._cooldown = cooldown
        self._actions = actions

    async def handle(
        self,
        message: NormalizedMessage,
        match: RuleMatch,
        *,
        chat_id: uuid.UUID | None,
        message_id: uuid.UUID | None,
    ) -> ReplyOutcome:
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

        analysis: AnalysisOutcome | None = None
        if rule.ai_enabled:
            if self._analyzer is None:
                return await self._escalate(
                    message, match, chat_id, message_id, "AI не настроен, нужен оператор"
                )

            scenario = await self._load_scenario(rule.scenario_id)
            analysis = await self._analyzer.analyze(
                system_prompt=scenario.system_prompt if scenario else rule.name,
                message_text=message.text,
                threshold=rule.ai_threshold or 0.0,
                rule_name=rule.name,
                model=scenario.model if scenario else None,
                account_id=message.account_id,
                message_id=message_id,
                scenario_id=rule.scenario_id,
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
                elif analysis.failed:
                    reason = analysis.failure_reason or "AI недоступен"
                else:
                    reason = (
                        f"низкая уверенность AI "
                        f"({analysis.result.confidence:.2f} < {analysis.threshold:.2f})"
                    )
                return await self._escalate(
                    message, match, chat_id, message_id, reason, analysis=analysis
                )

            # Хендофф выключен, но модель не уверена или отказалась — не выдумываем
            # ответ, просто пропускаем.
            if analysis.failed or not analysis.passes_threshold or not analysis.result.relevant:
                return await self._ignore(
                    message,
                    match,
                    chat_id,
                    message_id,
                    f"AI: не отвечать (confidence {analysis.result.confidence:.2f}, "
                    f"порог {analysis.threshold:.2f})",
                    analysis=analysis,
                )

        scenario = await self._load_scenario(rule.scenario_id)
        if scenario is None or self._generator is None:
            return await self._escalate(
                message, match, chat_id, message_id, "нет сценария для ответа", analysis=analysis
            )

        context = await self._context.build(
            account_id=message.account_id,
            tg_chat_id=message.tg_chat_id,
            peer_tg_id=message.sender_tg_id,
            current_tg_message_id=message.tg_message_id,
            context_messages=scenario.context_messages,
        )

        # Провайдер может отказать: перегрузка агрегатора, таймаут, обрыв сети.
        # Исключение здесь нельзя пускать наверх — сообщение просто исчезнет из
        # виду: ни ответа, ни отметки в панели. Обрабатываем так же, как пустой
        # ответ модели: подставляем заготовленный текст или зовём человека.
        generation = None
        generation_error: str | None = None
        try:
            generation = await self._generator.generate(
                _scenario_settings(scenario),
                message_text=message.text,
                context=context.history,
                knowledge=[],
                memory=context.memory,
                conversation_summary=context.summary,
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
        if generation is not None and generation.refused and not scenario.human_handoff_enabled:
            clarification = (
                generation.reply.refusal_reason
                if generation.reply and generation.reply.refusal_reason
                else None
            )
            if clarification and len(clarification.strip()) >= 8:
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
                    analysis=analysis,
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
            if scenario.fallback_text:
                # Заранее написанный человеком текст лучше выдуманного ответа.
                return await self._send(
                    message,
                    match,
                    chat_id,
                    message_id,
                    context,
                    scenario,
                    text=scenario.fallback_text,
                    used_knowledge=False,
                    cooldown_keys=cooldown_keys,
                    analysis=analysis,
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
            used_knowledge=generation.reply.used_knowledge,
            cooldown_keys=cooldown_keys,
            analysis=analysis,
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
        used_knowledge: bool,
        cooldown_keys: CooldownKeys,
        analysis: AnalysisOutcome | None,
    ) -> ReplyOutcome:
        verdict = self._validator.validate(
            ValidationContext(
                text=text,
                used_knowledge=used_knowledge,
                require_grounding=scenario.require_knowledge_grounding,
                max_length=scenario.max_reply_length or self._settings.max_message_length,
                recent_replies=context.recent_replies,
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

        allowed, claim = await self._cooldown.claim(cooldown_keys, match.rule.cooldown)
        if not allowed:
            return await self._ignore(
                message,
                match,
                chat_id,
                message_id,
                f"cooldown: {allowed.blocked_by}",
                analysis=analysis,
            )

        # Лид = тот, кому мы ответили хотя бы раз. Балл берём из уверенности
        # ИИ (0..100); без AI-проверки — базовый, чтобы лид всё равно завёлся.
        if analysis is not None:
            lead_score = int(round(analysis.result.confidence * 100))
            intent = analysis.result.intent.value
        else:
            lead_score = 40
            intent = None

        result = await self._actions.dispatch(
            ActionRequest(
                type=ActionType.REPLY,
                account_id=message.account_id,
                dedup_key=_dedup_key(message, ActionType.REPLY),
                message=message,
                chat_id=chat_id,
                message_id=message_id,
                conversation_id=context.conversation_id,
                rule_id=match.rule.id,
                scenario_id=scenario.id,
                reply_text=text,
                validation=verdict.to_payload(),
                payload={"lead_score": lead_score, "intent": intent},
            )
        )

        if result.status is not ActionStatus.SENT:
            # Ответ не ушёл — задержку надо снять, иначе следующая попытка
            # окажется заблокированной несостоявшимся ответом.
            await self._cooldown.release(claim)

        return ReplyOutcome(
            action=ActionType.REPLY,
            status=result.status,
            reason=result.detail,
            action_id=result.action_id,
            analysis=analysis,
            validation=verdict,
        )

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


def _scenario_settings(scenario: Scenario) -> ScenarioSettings:
    return ScenarioSettings(
        system_prompt=scenario.system_prompt,
        model=scenario.model,
        temperature=float(scenario.temperature) if scenario.temperature is not None else None,
        max_tokens=scenario.max_tokens,
        max_reply_length=scenario.max_reply_length,
        language=scenario.language,
        require_grounding=scenario.require_knowledge_grounding,
    )


def _dedup_key(message: NormalizedMessage, action: ActionType) -> str:
    """Одно действие одного типа на одно сообщение — не больше."""
    account_id, tg_chat_id, tg_message_id = message.dedup_key
    return f"{action.value}:{account_id}:{tg_chat_id}:{tg_message_id}"
