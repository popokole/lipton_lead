"""Генерация ответа (ТЗ §13, §17).

Генератор не решает, отвечать ли — это уже решено правилом и анализатором. Его
задача: собрать промпт из сценария и контекста, получить текст и честно
сообщить, если ответить не получилось.

Отказ модели (`refused`) — это результат, а не ошибка. Сценарий с обязательной
опорой на базу знаний обязан молчать, когда нужных сведений нет: выдуманный
ответ клиенту хуже отсутствия ответа.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.ai.budget import AIBudget, UsageRecorder
from app.ai.provider import (
    AIProvider,
    ChatMessage,
    GeneratedReply,
    GenerateRequest,
    SummarizeRequest,
    Summary,
    Usage,
)
from app.core.errors import AIBudgetExceededError, AIError
from app.core.logging import get_logger
from app.database.session import Database
from app.models import AIPurpose

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class GenerationOutcome:
    reply: GeneratedReply | None
    usage: Usage
    failed: bool = False
    failure_reason: str | None = None

    @property
    def has_text(self) -> bool:
        return self.reply is not None and bool(self.reply.text.strip())

    @property
    def refused(self) -> bool:
        return self.failed or (self.reply is not None and self.reply.refused)


@dataclass(frozen=True, slots=True)
class ScenarioSettings:
    """Параметры сценария, которые влияют на генерацию."""

    system_prompt: str
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    max_reply_length: int | None = None
    language: str | None = None
    require_grounding: bool = False


class AIGenerator:
    def __init__(
        self,
        provider: AIProvider,
        budget: AIBudget,
        recorder: UsageRecorder,
        database: Database,
    ) -> None:
        self._provider = provider
        self._budget = budget
        self._recorder = recorder
        self._database = database

    async def generate(
        self,
        scenario: ScenarioSettings,
        *,
        message_text: str,
        context: list[ChatMessage] | None = None,
        knowledge: list[str] | None = None,
        memory: dict[str, str] | None = None,
        conversation_summary: str | None = None,
        account_id: uuid.UUID | None = None,
        message_id: uuid.UUID | None = None,
        scenario_id: uuid.UUID | None = None,
    ) -> GenerationOutcome:
        knowledge = knowledge or []
        if scenario.require_grounding and not knowledge:
            # Обращаться к модели незачем: сценарий запрещает отвечать без
            # подтверждения из базы знаний.
            logger.info("generation_skipped_without_knowledge", scenario_id=str(scenario_id))
            return GenerationOutcome(
                reply=None,
                usage=Usage(),
                failed=True,
                failure_reason="в базе знаний нет данных для ответа",
            )

        request = GenerateRequest(
            system_prompt=scenario.system_prompt,
            message_text=message_text,
            context=context or [],
            knowledge=knowledge,
            memory=memory or {},
            conversation_summary=conversation_summary,
            require_grounding=scenario.require_grounding,
            max_reply_length=scenario.max_reply_length,
            model=scenario.model,
            temperature=scenario.temperature,
            max_tokens=scenario.max_tokens,
        )

        try:
            async with self._budget.slot():
                response = await self._provider.generate(request)
        except AIBudgetExceededError as exc:
            logger.warning("ai_budget_exhausted", detail=exc.message)
            return GenerationOutcome(
                reply=None, usage=Usage(), failed=True, failure_reason=exc.message
            )
        except AIError as exc:
            logger.warning("ai_generation_failed", detail=exc.message)
            await self._record(
                AIPurpose.GENERATE,
                Usage(model=scenario.model or ""),
                account_id=account_id,
                message_id=message_id,
                scenario_id=scenario_id,
                status="ERROR",
                error=exc.message,
            )
            return GenerationOutcome(
                reply=None, usage=Usage(), failed=True, failure_reason=exc.message
            )

        await self._record(
            AIPurpose.GENERATE,
            response.usage,
            account_id=account_id,
            message_id=message_id,
            scenario_id=scenario_id,
        )
        logger.info(
            "ai_generation",
            refused=response.result.refused,
            used_knowledge=response.result.used_knowledge,
            length=len(response.result.text),
            tokens=response.usage.total_tokens,
            latency_ms=response.usage.latency_ms,
        )
        return GenerationOutcome(reply=response.result, usage=response.usage)

    async def summarize(
        self,
        messages: list[ChatMessage],
        *,
        previous_summary: str | None = None,
        language: str | None = None,
        model: str | None = None,
        account_id: uuid.UUID | None = None,
    ) -> Summary | None:
        """Пересказ старой части переписки. None — если не удалось."""
        if not messages:
            return None

        request = SummarizeRequest(
            messages=messages,
            previous_summary=previous_summary,
            language=language,
            model=model,
        )
        try:
            async with self._budget.slot():
                response = await self._provider.summarize(request)
        except (AIError, AIBudgetExceededError) as exc:
            logger.warning("ai_summary_failed", detail=exc.message)
            return None

        await self._record(
            AIPurpose.SUMMARIZE, response.usage, account_id=account_id, message_id=None
        )
        return response.result

    async def _record(
        self,
        purpose: AIPurpose,
        usage: Usage,
        *,
        account_id: uuid.UUID | None,
        message_id: uuid.UUID | None,
        scenario_id: uuid.UUID | None = None,
        status: str = "OK",
        error: str | None = None,
    ) -> None:
        try:
            async with self._database.session() as db:
                cost = await self._recorder.record(
                    db,
                    provider=self._provider.name,
                    purpose=purpose,
                    usage=usage,
                    account_id=account_id,
                    message_id=message_id,
                    scenario_id=scenario_id,
                    status=status,
                    error=error,
                )
            await self._budget.charge(cost)
        except Exception as exc:  # noqa: BLE001 — учёт не важнее обработки
            logger.warning("ai_usage_record_failed", detail=str(exc))
