"""Резерв основного AI-провайдера (ТЗ §12).

Оборачивает основной провайдер: при его сбое (перегрузка, битый ответ,
исчерпан бюджет — всё, что наследуется от AIError) пробует резервный.
Конвейеру и правилам это прозрачно — они видят один AIProvider, как раньше.

Резерв применяется только к analyze/generate: это то, что реально стоит на
пути ответа собеседнику и падает под нагрузкой (см. инцидент 2026-08-22 —
запросы на впн/психолога молча терялись, пока codex.sale был перегружен).
summarize — фоновая задача, не блокирует ответ; embed резервным провайдером
может вообще не поддерживаться (см. AnthropicProvider.embed). Оба идут
только через основной, чтобы не усложнять путь, который и так не критичен.
"""

from __future__ import annotations

from app.ai.provider import (
    AIProvider,
    AIResponse,
    AnalysisResult,
    AnalyzeRequest,
    GeneratedReply,
    GenerateRequest,
    SummarizeRequest,
    Summary,
)
from app.core.errors import AIError
from app.core.logging import get_logger

logger = get_logger(__name__)


class FallbackProvider:
    def __init__(self, primary: AIProvider, secondary: AIProvider) -> None:
        self._primary = primary
        self._secondary = secondary

    @property
    def name(self) -> str:
        return f"{self._primary.name}+fallback:{self._secondary.name}"

    async def close(self) -> None:
        await self._primary.close()
        await self._secondary.close()

    async def analyze(self, request: AnalyzeRequest) -> AIResponse[AnalysisResult]:
        try:
            return await self._primary.analyze(request)
        except AIError as exc:
            logger.warning(
                "ai_fallback", op="analyze", primary=self._primary.name, detail=str(exc)
            )
            return await self._secondary.analyze(request)

    async def generate(self, request: GenerateRequest) -> AIResponse[GeneratedReply]:
        try:
            return await self._primary.generate(request)
        except AIError as exc:
            logger.warning(
                "ai_fallback", op="generate", primary=self._primary.name, detail=str(exc)
            )
            return await self._secondary.generate(request)

    async def summarize(self, request: SummarizeRequest) -> AIResponse[Summary]:
        return await self._primary.summarize(request)

    async def embed(
        self, texts: list[str], model: str | None = None
    ) -> AIResponse[list[list[float]]]:
        return await self._primary.embed(texts, model)
