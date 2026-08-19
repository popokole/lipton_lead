"""Анализ релевантности сообщения (ТЗ §11).

Вызывается только после того, как дешёвые фильтры уже отсеяли явно
неподходящее. Задача анализатора — не «понять сообщение», а ответить на три
вопроса: относится ли оно к делу, стоит ли отвечать и не нужен ли здесь
человек.

Если модель недоступна или бюджет исчерпан, анализатор не роняет обработку:
он возвращает решение «не отвечать» с пометкой о причине. Тишина здесь
безопаснее, чем ответ, отправленный вслепую.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.ai.budget import AIBudget, UsageRecorder
from app.ai.provider import (
    AIProvider,
    AnalysisResult,
    AnalyzeRequest,
    ChatMessage,
    Intent,
    Usage,
)
from app.core.errors import AIBudgetExceededError, AIError
from app.core.logging import get_logger
from app.database.session import Database
from app.models import AIPurpose

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AnalysisOutcome:
    """Решение анализатора вместе с причиной, понятной оператору."""

    result: AnalysisResult
    usage: Usage
    threshold: float
    failed: bool = False
    failure_reason: str | None = None

    @property
    def passes_threshold(self) -> bool:
        return self.result.confidence >= self.threshold

    @property
    def should_reply(self) -> bool:
        return self.result.is_actionable and self.passes_threshold

    @property
    def needs_human(self) -> bool:
        """Человек нужен и когда модель так решила, и когда она не уверена."""
        if self.failed:
            return True
        if self.result.needs_human:
            return True
        return self.result.relevant and self.result.should_reply and not self.passes_threshold


class AIAnalyzer:
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

    async def analyze(
        self,
        *,
        system_prompt: str,
        message_text: str,
        threshold: float,
        context: list[ChatMessage] | None = None,
        rule_name: str | None = None,
        model: str | None = None,
        account_id: uuid.UUID | None = None,
        message_id: uuid.UUID | None = None,
        scenario_id: uuid.UUID | None = None,
    ) -> AnalysisOutcome:
        request = AnalyzeRequest(
            system_prompt=system_prompt,
            message_text=message_text,
            context=context or [],
            rule_name=rule_name,
            model=model,
        )

        try:
            async with self._budget.slot():
                response = await self._provider.analyze(request)
        except AIBudgetExceededError as exc:
            logger.warning("ai_budget_exhausted", detail=exc.message)
            return self._failure(threshold, exc.message)
        except AIError as exc:
            logger.warning("ai_analysis_failed", detail=exc.message)
            await self._record(
                Usage(model=model or ""),
                account_id=account_id,
                message_id=message_id,
                scenario_id=scenario_id,
                status="ERROR",
                error=exc.message,
            )
            return self._failure(threshold, exc.message)

        await self._record(
            response.usage,
            account_id=account_id,
            message_id=message_id,
            scenario_id=scenario_id,
        )

        logger.info(
            "ai_analysis",
            relevant=response.result.relevant,
            confidence=round(response.result.confidence, 3),
            intent=response.result.intent.value,
            threshold=threshold,
            tokens=response.usage.total_tokens,
            latency_ms=response.usage.latency_ms,
        )
        return AnalysisOutcome(result=response.result, usage=response.usage, threshold=threshold)

    @staticmethod
    def _failure(threshold: float, reason: str) -> AnalysisOutcome:
        """Модель недоступна — решаем не отвечать и звать человека."""
        return AnalysisOutcome(
            result=AnalysisResult(
                relevant=False,
                confidence=0.0,
                intent=Intent.OTHER,
                should_reply=False,
                needs_human=True,
                reason=reason,
            ),
            usage=Usage(),
            threshold=threshold,
            failed=True,
            failure_reason=reason,
        )

    async def _record(
        self,
        usage: Usage,
        *,
        account_id: uuid.UUID | None,
        message_id: uuid.UUID | None,
        scenario_id: uuid.UUID | None,
        status: str = "OK",
        error: str | None = None,
    ) -> None:
        try:
            async with self._database.session() as db:
                cost = await self._recorder.record(
                    db,
                    provider=self._provider.name,
                    purpose=AIPurpose.ANALYZE,
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
