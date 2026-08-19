"""Реализация поставщика на OpenAI-совместимом API (ТЗ §12).

Работает и с самим OpenAI, и с любым совместимым сервисом через
OPENAI_BASE_URL — ключи и адрес приходят из окружения, в коде их нет.

Ответы запрашиваются в режиме structured output по схеме Pydantic. Если сервис
такой режим не поддерживает, происходит откат на разбор JSON из текста: это
хуже, но лучше, чем гадать по свободному тексту.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from pydantic import BaseModel, ValidationError

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
    json_schema_of,
)
from app.ai.responses_transport import ResponsesTransport
from app.core.config import Settings
from app.core.errors import AIError, AIResponseFormatError
from app.core.logging import get_logger

logger = get_logger(__name__)


class OpenAIProvider:
    def __init__(self, settings: Settings) -> None:
        if settings.openai_api_key is None:
            raise AIError("OPENAI_API_KEY is not configured")

        self._settings = settings
        self._responses: ResponsesTransport | None = None
        self._client: Any = None

        if settings.ai_wire_api == "responses":
            if not settings.openai_base_url:
                raise AIError("Для AI_WIRE_API=responses нужен OPENAI_BASE_URL")
            self._responses = ResponsesTransport(
                base_url=settings.openai_base_url,
                api_key=settings.openai_api_key.get_secret_value(),
                timeout=settings.ai_timeout_seconds,
                reasoning_effort=settings.ai_reasoning_effort,
                max_attempts=settings.ai_retry_attempts,
                proxy=settings.ai_proxy_url,
                total_budget=settings.ai_total_budget_seconds,
            )
        else:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                api_key=settings.openai_api_key.get_secret_value(),
                base_url=settings.openai_base_url,
                timeout=settings.ai_timeout_seconds,
                max_retries=2,
            )

    @property
    def name(self) -> str:
        return "openai"

    async def close(self) -> None:
        if self._responses is not None:
            await self._responses.close()
        if self._client is not None:
            await self._client.close()

    # --- операции ----------------------------------------------------------
    async def analyze(self, request: AnalyzeRequest) -> AIResponse[AnalysisResult]:
        return await self._structured(
            build_analyze_messages(request),
            AnalysisResult,
            model=request.model,
            # Анализ должен быть воспроизводимым: одно и то же сообщение не
            # может то проходить фильтр, то нет.
            temperature=0.0,
            max_tokens=request.max_tokens or 400,
        )

    async def generate(self, request: GenerateRequest) -> AIResponse[GeneratedReply]:
        """Генерация ответа собеседнику.

        В отличие от анализа, здесь строгий JSON — не самоцель: результат всё
        равно уходит человеку как обычный текст. Агрегаторы, проксирующие
        «агентов», охотно отвечают простой фразой, и ломать из-за этого ответ
        глупо — если разбор не удался, берём текст как есть.
        """
        try:
            return await self._structured(
                build_generate_messages(request),
                GeneratedReply,
                model=request.model,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
            )
        except AIResponseFormatError as exc:
            raw = getattr(exc, "raw_text", "").strip()
            if not raw:
                raise
            logger.info("ai_generation_plain_text", length=len(raw))
            return AIResponse(
                result=GeneratedReply(text=raw),
                usage=getattr(exc, "usage", None) or Usage(model=request.model or "", latency_ms=0),
            )

    async def summarize(self, request: SummarizeRequest) -> AIResponse[Summary]:
        return await self._structured(
            build_summarize_messages(request),
            Summary,
            model=request.model,
            temperature=0.2,
            max_tokens=600,
        )

    async def embed(
        self, texts: list[str], model: str | None = None
    ) -> AIResponse[list[list[float]]]:
        if not texts:
            return AIResponse(result=[], usage=Usage())

        started = time.perf_counter()
        chosen = model or self._settings.embedding_model
        try:
            response = await self._client.embeddings.create(model=chosen, input=texts)
        except Exception as exc:
            raise AIError(f"embedding request failed: {type(exc).__name__}: {exc}") from exc

        vectors = [item.embedding for item in response.data]
        expected = self._settings.embedding_dim
        for vector in vectors:
            if len(vector) != expected:
                raise AIError(
                    f"embedding model {chosen} returned {len(vector)} dimensions, "
                    f"expected {expected}"
                )

        usage = Usage(
            prompt_tokens=getattr(response.usage, "prompt_tokens", 0),
            model=chosen,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        return AIResponse(result=vectors, usage=usage)

    # --- внутреннее --------------------------------------------------------
    async def _structured[T: BaseModel](
        self,
        messages: list[ChatMessage],
        schema: type[T],
        *,
        model: str | None,
        temperature: float | None,
        max_tokens: int | None,
    ) -> AIResponse[T]:
        chosen_model = model or self._settings.default_ai_model
        started = time.perf_counter()

        payload: dict[str, Any] = {
            "model": chosen_model,
            "messages": [message.model_dump() for message in messages],
            "max_completion_tokens": max_tokens or self._settings.default_ai_max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": json_schema_of(schema),
                    "strict": True,
                },
            },
        }
        if temperature is not None:
            payload["temperature"] = temperature

        if self._responses is not None:
            # Агрегатор не понимает response_format, поэтому формат держим
            # промптом: явно перечисляем поля JSON. Без этого модель отвечает
            # прозой, и text/group_text не разделяются.
            hinted = [
                *messages,
                ChatMessage(role="system", content=_json_instruction(schema)),
            ]
            result = await self._responses.complete(
                messages=[message.model_dump() for message in hinted],
                model=chosen_model,
                max_output_tokens=max_tokens or self._settings.default_ai_max_tokens,
                temperature=temperature,
                fallback_models=self._settings.ai_fallback_models,
            )
            latency_ms = int((time.perf_counter() - started) * 1000)
            return AIResponse(
                result=parse_structured(result.text, schema),
                usage=Usage(
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    model=chosen_model,
                    latency_ms=latency_ms,
                ),
            )

        try:
            response = await self._client.chat.completions.create(**payload)
        except Exception as exc:
            raise AIError(f"{type(exc).__name__}: {exc}") from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        usage = Usage(
            prompt_tokens=getattr(response.usage, "prompt_tokens", 0) if response.usage else 0,
            completion_tokens=(
                getattr(response.usage, "completion_tokens", 0) if response.usage else 0
            ),
            model=chosen_model,
            latency_ms=latency_ms,
        )

        content = response.choices[0].message.content if response.choices else None
        if not content:
            raise AIResponseFormatError("AI returned an empty response")

        return AIResponse(result=parse_structured(content, schema), usage=usage)


_TYPE_HINTS = {str: "строка", bool: "true/false", int: "целое", float: "число"}


def _json_instruction(schema: type[BaseModel]) -> str:
    """Инструкция для агрегаторов без response_format: поля JSON с типами.

    Типы важны: без них модель кладёт прозу в булевы поля вроде used_knowledge,
    и строгая валидация ломается.
    """
    parts = []
    for name, field in schema.model_fields.items():
        hint = _TYPE_HINTS.get(field.annotation, "строка")  # type: ignore[arg-type]
        parts.append(f"{name} ({hint})")
    fields = ", ".join(parts)
    return (
        "Верни ТОЛЬКО один JSON-объект без пояснений и без ``` "
        f"строго с этими полями и типами: {fields}. "
        "Не добавляй других полей. Соблюдай типы: булевы поля — true/false, "
        "не текст. Переносы строк внутри значений экранируй как \\n."
    )


def parse_structured[T: BaseModel](content: str, schema: type[T]) -> T:
    """Разбирает ответ модели по схеме.

    Некоторые совместимые сервисы оборачивают JSON в ```-блок или добавляют
    пояснения до и после. Вытаскиваем сам объект, но не пытаемся угадывать
    отсутствующие поля — их отсутствие означает, что ответу нельзя доверять.
    """
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        return schema.model_validate_json(text)
    except ValidationError:
        pass

    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise AIResponseFormatError(
            f"AI response is not valid JSON for {schema.__name__}", raw_text=text
        )

    try:
        data = json.loads(text[start : end + 1])
        return schema.model_validate(data)
    except (json.JSONDecodeError, ValidationError):
        pass

    # Последняя попытка: модели любят класть в JSON-строки живые переносы
    # строк, на которых строгий json.loads падает. Достаём строковые поля
    # схемы регуляркой — так «text»/«group_text» не утекут сырым JSON в чат.
    salvaged = _salvage_fields(text, schema)
    if salvaged:
        try:
            return schema.model_validate(salvaged)
        except ValidationError:
            pass

    raise AIResponseFormatError(
        f"AI response does not match {schema.__name__}", raw_text=text
    )


_STRING_FIELD = r'"{name}"\s*:\s*"((?:[^"\\]|\\.)*)"'


def _salvage_fields[T: BaseModel](raw: str, schema: type[T]) -> dict[str, str]:
    """Вытаскивает строковые поля схемы из кривого JSON регуляркой.

    Модели любят класть в значения живые переносы строк — на них строгий
    json.loads падает. Регулярка с re.S захватывает такие значения целиком,
    после чего пробуем аккуратно распаковать escape-последовательности.
    """
    out: dict[str, str] = {}
    for name, field in schema.model_fields.items():
        # Только строковые поля: булев used_knowledge модель может заполнить
        # прозой, и попытка втащить его сюда сорвала бы восстановление text.
        if field.annotation is not str:
            continue
        pattern = _STRING_FIELD.format(name=re.escape(name))
        match = re.search(pattern, raw, re.S)
        if not match:
            continue
        value = match.group(1)
        try:
            out[name] = json.loads(f'"{value}"')
        except json.JSONDecodeError:
            out[name] = value.replace('\\"', '"')
    return out
