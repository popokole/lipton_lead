"""Транспорт Responses API: ретраи на нестабильность шлюза (ТЗ §12)."""

from __future__ import annotations

import httpx
import pytest

from app.ai.responses_transport import ResponsesTransport, TransportUnstableError

_DELTA_EVENT = b'data: {"type": "response.output_text.delta", "delta": "\xd0\xbf\xd1\x80\xd0\xb8\xd0\xb2\xd0\xb5\xd1\x82"}\n\n'  # noqa: E501
_DONE_EVENT = (
    b'data: {"type": "response.completed", '
    b'"response": {"usage": {"input_tokens": 5, "output_tokens": 2}}}\n\n'
)
_SSE_OK = _DELTA_EVENT + _DONE_EVENT + b"data: [DONE]\n\n"


def _transport(handler: httpx.MockTransport) -> ResponsesTransport:
    rt = ResponsesTransport(
        base_url="https://codex.example/backend-api/codex",
        api_key="test-key",
        timeout=5.0,
        max_attempts=3,
        total_budget=30.0,
    )
    rt._client = httpx.AsyncClient(transport=handler)
    return rt


class TestGatewayRetry:
    async def test_502_is_retried_and_succeeds(self) -> None:
        calls = {"n": 0}

        def handle(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(502, content=b'{"error":{"message":"upstream down"}}')
            return httpx.Response(
                200, content=_SSE_OK, headers={"content-type": "text/event-stream"}
            )

        rt = _transport(httpx.MockTransport(handle))
        result = await rt.complete(
            messages=[{"role": "user", "content": "hi"}], model="m", max_output_tokens=100
        )
        assert result.text == "привет"
        assert calls["n"] == 2
        await rt.close()

    async def test_400_is_not_retried(self) -> None:
        calls = {"n": 0}

        def handle(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(400, content=b'{"error":{"message":"bad request"}}')

        rt = _transport(httpx.MockTransport(handle))
        with pytest.raises(Exception) as exc_info:
            await rt.complete(
                messages=[{"role": "user", "content": "hi"}], model="m", max_output_tokens=100
            )
        assert not isinstance(exc_info.value, TransportUnstableError)
        assert calls["n"] == 1
        await rt.close()

    async def test_upstream_unavailable_error_code_is_retried_and_succeeds(self) -> None:
        """Регрессия 2026-09-06: агрегатор шлёт 200 OK + SSE-событие error с
        code=upstream_unavailable (сам текст: "Повторите запрос") — раньше
        это падало необрабатываемым AIError с первой попытки."""
        calls = {"n": 0}

        def handle(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                body = (
                    b'data: {"type": "error", "error": {"code": "upstream_unavailable", '
                    b'"message": "try again"}}\n\n'
                )
                return httpx.Response(
                    200, content=body, headers={"content-type": "text/event-stream"}
                )
            return httpx.Response(
                200, content=_SSE_OK, headers={"content-type": "text/event-stream"}
            )

        rt = _transport(httpx.MockTransport(handle))
        result = await rt.complete(
            messages=[{"role": "user", "content": "hi"}], model="m", max_output_tokens=100
        )
        assert result.text == "привет"
        assert calls["n"] == 2
        await rt.close()

    async def test_persistent_502_exhausts_retries(self) -> None:
        calls = {"n": 0}

        def handle(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(502, content=b'{"error":{"message":"still down"}}')

        rt = _transport(httpx.MockTransport(handle))
        with pytest.raises(TransportUnstableError):
            await rt.complete(
                messages=[{"role": "user", "content": "hi"}], model="m", max_output_tokens=100
            )
        assert calls["n"] == 3
        await rt.close()
