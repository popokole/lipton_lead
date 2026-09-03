"""Anthropic (Claude) как резервный поставщик AI (ТЗ §12).

Не заменяет основной провайдер — используется только как резерв, когда он
падает (см. FallbackProvider в app/ai/fallback_provider.py). Отдельный HTTP-
путь до api.anthropic.com: сбой codex.sale/агрегатора на него не влияет,
в отличие от «резервных моделей» AI_FALLBACK_MODELS, которые все идут через
тот же самый агрегатор.

structured output собирается тем же способом, что и у OpenAIProvider для
режима responses (агрегатор): промпт с явным перечислением полей + разбор
через parse_structured. Свой формат ответа (output_config.format) не
используется намеренно — эта реализация нужна редко, только при сбое
основного провайдера, и должна быть максимально простой и предсказуемой,
а не самой эффективной.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from anthropic.types import MessageParam

from app.ai.openai_provider import json_instruction, parse_structured
from app.ai.prompts import (
    build_analyze_messages,
    build_generate_messages,
    build_summarize_messages,
)
from app.ai.provider import (
    AIResponse,
    AnalysisResult,
    AnalyzeRequest,
    ChatMessage,
    GeneratedReply,
    GenerateRequest,
    SummarizeRequest,
    Summary,
    Usage,
)
from app.core.config import Settings
from app.core.errors import AIError
from app.core.logging import get_logger

logger = get_logger(__name__)


class AnthropicProvider:
    def __init__(self, settings: Settings) -> None:
        if settings.anthropic_api_key is None:
            raise AIError("ANTHROPIC_API_KEY is not configured")

        import anthropic

        self._settings = settings
        self._model = settings.anthropic_model
        self._client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            timeout=settings.ai_timeout_seconds,
            max_retries=1,
        )

    @property
    def name(self) -> str:
        return "anthropic"

    async def close(self) -> None:
        await self._client.close()

    # --- операции ------------------------------------------------------
    async def analyze(self, request: AnalyzeRequest) -> AIResponse[AnalysisResult]:
        return await self._structured(
            build_analyze_messages(request), AnalysisResult, max_tokens=request.max_tokens or 400
        )

    async def generate(self, request: GenerateRequest) -> AIResponse[GeneratedReply]:
        return await self._structured(
            build_generate_messages(request),
            GeneratedReply,
            max_tokens=request.max_tokens or self._settings.default_ai_max_tokens,
        )

    async def summarize(self, request: SummarizeRequest) -> AIResponse[Summary]:
        return await self._structured(build_summarize_messages(request), Summary, max_tokens=600)

    async def embed(
        self, texts: list[str], model: str | None = None
    ) -> AIResponse[list[list[float]]]:
        # Anthropic не отдаёт эмбеддинги отдельным эндпоинтом — этот путь
        # резервным провайдером не покрыт, база знаний тут не при чём.
        raise AIError("Anthropic provider does not support embeddings")

    # --- внутреннее ------------------------------------------------------
    async def _structured[T: BaseModel](
        self, messages: list[ChatMessage], schema: type[T], *, max_tokens: int
    ) -> AIResponse[T]:
        # Anthropic отделяет system от messages — в отличие от OpenAI-формата,
        # где все системные реплики идут вперемешку с user/assistant внутри
        # одного списка. Билдеры промптов кладут все system-реплики впереди
        # user/assistant, поэтому просто разносим по роли.
        system_text = "\n\n".join(m.content for m in messages if m.role == "system")
        system_text = f"{system_text}\n\n{json_instruction(schema)}".strip()
        conversation: list[MessageParam] = [
            {"role": m.role, "content": m.content}  # type: ignore[typeddict-item]
            for m in messages
            if m.role != "system"
        ]

        started = time.perf_counter()
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=system_text,
                messages=conversation,
            )
        except Exception as exc:
            raise AIError(f"{type(exc).__name__}: {exc}") from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        text = "".join(block.text for block in response.content if block.type == "text")
        usage = Usage(
            prompt_tokens=response.usage.input_tokens,
            completion_tokens=response.usage.output_tokens,
            model=self._model,
            latency_ms=latency_ms,
        )
        return AIResponse(result=parse_structured(text, schema), usage=usage)
