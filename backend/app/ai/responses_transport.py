"""Транспорт под OpenAI Responses API.

Нужен для агрегаторов вроде codex.sale, у которых нет `/chat/completions` —
только `/responses`. Отличий от привычного клиента три, и каждое ломает
обычный SDK:

1. Ответ всегда приходит потоком SSE, даже если попросить `stream: false`.
   Поэтому тело читается построчно, а не как единый JSON.
2. Перегрузка приходит не HTTP-кодом, а событием `response.failed` с кодом
   `service_busy` внутри потока со статусом 200. Без разбора событий такая
   ошибка выглядит как пустой ответ модели.
3. Агрегатор может не поддерживать строгую JSON-схему, поэтому формат ответа
   задаётся промптом, а разбор делает наш терпимый парсер.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.errors import AIError
from app.core.logging import get_logger

logger = get_logger(__name__)

# Коды, при которых повтор осмыслен: агрегатор просит подождать.
RETRYABLE_CODES = frozenset({"service_busy", "rate_limit_error", "server_error"})


@dataclass(frozen=True, slots=True)
class ResponsesResult:
    text: str
    prompt_tokens: int
    completion_tokens: int


class RetryableAIError(AIError):
    """Сбой, который проходит сам: повтор осмыслен."""


class ServiceBusyError(RetryableAIError):
    """Агрегатор перегружен."""

    code = "ai_service_busy"


class TransportUnstableError(RetryableAIError):
    """Сеть до провайдера моргнула: таймаут, обрыв, отказ соединения."""

    code = "ai_transport_unstable"


class ResponsesTransport:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: float,
        reasoning_effort: str | None = None,
        max_attempts: int = 3,
        proxy: str | None = None,
        total_budget: float = 60.0,
    ) -> None:
        self._url = base_url.rstrip("/") + "/responses"
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        self._timeout = timeout
        self._reasoning_effort = reasoning_effort
        self._max_attempts = max_attempts
        self._total_budget = total_budget
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout), proxy=proxy)

    async def close(self) -> None:
        await self._client.aclose()

    async def complete(
        self,
        *,
        messages: list[dict[str, str]],
        model: str,
        max_output_tokens: int,
        temperature: float | None = None,
        fallback_models: list[str] | None = None,
    ) -> ResponsesResult:
        """Пробует модели по очереди, пока одна не ответит.

        У агрегатора десятки моделей, и заняты они по-разному: пока основная
        отвечает «перегружен», соседняя спокойно генерирует.

        Перебор ограничен общим сроком. Без него повторы и список моделей
        перемножаются: пять моделей по четыре попытки с растущими паузами —
        это минуты на одно сообщение, а собеседник столько ждать не будет.
        """
        chain = [model, *(fallback_models or [])]
        last: Exception | None = None
        deadline = time.monotonic() + self._total_budget

        for candidate in chain:
            if time.monotonic() >= deadline:
                logger.warning("ai_budget_exhausted", tried=chain.index(candidate))
                break
            try:
                return await self._complete_one(
                    messages=messages,
                    model=candidate,
                    max_output_tokens=max_output_tokens,
                    temperature=temperature,
                    deadline=deadline,
                )
            except ServiceBusyError as exc:
                last = exc
                if candidate != chain[-1]:
                    logger.info("ai_model_switch", busy=candidate)
            except TransportUnstableError as exc:
                # Смена модели от сетевого сбоя не спасёт — он общий для всех.
                raise exc from None

        raise last or AIError("AI request failed")

    async def _complete_one(
        self,
        *,
        messages: list[dict[str, str]],
        model: str,
        max_output_tokens: int,
        temperature: float | None,
        deadline: float,
    ) -> ResponsesResult:
        payload: dict[str, Any] = {
            "model": model,
            "input": messages,
            "max_output_tokens": max_output_tokens,
            "stream": True,
        }
        if self._reasoning_effort:
            payload["reasoning"] = {"effort": self._reasoning_effort}
        if temperature is not None:
            payload["temperature"] = temperature

        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                # Отдельный запрос не должен пережить общий срок: иначе
                # «бюджет 60 секунд» превращается в 60 плюс таймаут висящего
                # соединения.
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                return await self._request(payload, budget=remaining)
            except RetryableAIError as exc:
                last_error = exc
                if attempt == self._max_attempts or time.monotonic() >= deadline:
                    break
                # Агрегатор просит подождать «минуту». Столько ждать нельзя,
                # но и 2-3 секунды бессмысленны: под нагрузкой он отвечает
                # отказом сразу. Растём 5 → 10 → 20 с джиттером, чтобы
                # несколько сообщений не долбились в него в такт.
                delay = min(3 * attempt, 8) * (0.75 + random.random() * 0.5)
                delay = min(delay, max(deadline - time.monotonic(), 0.0))
                if delay <= 0:
                    break
                logger.warning(
                    "ai_retry",
                    attempt=attempt,
                    retry_in=round(delay, 1),
                    reason=type(exc).__name__,
                )
                await asyncio.sleep(delay)

        raise last_error or AIError("AI request failed")

    async def _request(self, payload: dict[str, Any], *, budget: float) -> ResponsesResult:
        try:
            async with self._client.stream(
                "POST",
                self._url,
                headers=self._headers,
                json=payload,
                timeout=httpx.Timeout(min(budget, self._timeout)),
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")[:300]
                    raise AIError(f"HTTP {response.status_code}: {body}")
                return await self._read_stream(response)
        except httpx.HTTPError as exc:
            # Обрыв на полпути — не приговор ответу: сеть до агрегатора
            # нестабильна, и следующая попытка обычно проходит.
            raise TransportUnstableError(f"{type(exc).__name__}: {exc}") from exc

    async def _read_stream(self, response: httpx.Response) -> ResponsesResult:
        chunks: list[str] = []
        final: dict[str, Any] | None = None

        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue  # комментарии-keepalive и заголовки событий
            raw = line[5:].strip()
            if not raw or raw == "[DONE]":
                continue

            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue

            kind = event.get("type", "")
            if kind == "response.output_text.delta":
                delta = event.get("delta")
                if isinstance(delta, str):
                    chunks.append(delta)
            elif kind in {"response.completed", "response.incomplete"}:
                final = event.get("response")
            elif kind in {"response.failed", "error"}:
                raise _failure_error(event)

        text = "".join(chunks).strip() or _text_from_response(final)
        if not text:
            raise AIError("AI вернул пустой ответ")

        usage = (final or {}).get("usage") or {}
        return ResponsesResult(
            text=text,
            prompt_tokens=int(usage.get("input_tokens", 0) or 0),
            completion_tokens=int(usage.get("output_tokens", 0) or 0),
        )


def _failure_error(event: dict[str, Any]) -> AIError:
    error = event.get("error") or (event.get("response") or {}).get("error") or {}
    code = str(error.get("code") or error.get("type") or "")
    message = str(error.get("message") or "AI provider reported a failure")

    if code in RETRYABLE_CODES:
        return ServiceBusyError(message)
    return AIError(f"{code or 'error'}: {message}")


def _text_from_response(response: dict[str, Any] | None) -> str:
    """Достаёт текст из финального события, если дельты не приходили."""
    if not response:
        return ""

    direct = response.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    parts: list[str] = []
    for item in response.get("output") or []:
        if not isinstance(item, dict):
            continue
        for block in item.get("content") or []:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
    return "".join(parts).strip()
