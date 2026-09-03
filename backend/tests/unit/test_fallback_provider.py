"""FallbackProvider: резерв срабатывает на AIError, а не на любой сбой."""

from __future__ import annotations

import pytest

from app.ai.fallback_provider import FallbackProvider
from app.ai.provider import AnalyzeRequest, GeneratedReply, GenerateRequest
from app.core.errors import AIError
from tests.fakes import FakeAIProvider


async def test_analyze_uses_primary_when_it_succeeds() -> None:
    primary = FakeAIProvider()
    secondary = FakeAIProvider()
    provider = FallbackProvider(primary, secondary)

    await provider.analyze(AnalyzeRequest(system_prompt="x", message_text="привет"))

    assert primary.analyze_calls == ["привет"]
    assert secondary.analyze_calls == []


async def test_analyze_falls_back_on_primary_ai_error() -> None:
    primary = FakeAIProvider(fail_analyze=AIError("перегружен"))
    secondary = FakeAIProvider()
    provider = FallbackProvider(primary, secondary)

    response = await provider.analyze(AnalyzeRequest(system_prompt="x", message_text="привет"))

    assert primary.analyze_calls == ["привет"]
    assert secondary.analyze_calls == ["привет"]
    assert response.result.relevant is True


async def test_generate_falls_back_on_primary_ai_error() -> None:
    primary = FakeAIProvider(fail_generate=AIError("перегружен"))
    secondary = FakeAIProvider(reply=GeneratedReply(text="ответ от резерва"))
    provider = FallbackProvider(primary, secondary)

    response = await provider.generate(GenerateRequest(system_prompt="x", message_text="привет"))

    assert response.result.text == "ответ от резерва"


async def test_non_ai_error_is_not_swallowed_by_fallback() -> None:
    """Резерв ловит только AIError — программную ошибку прятать нельзя."""
    primary = FakeAIProvider(fail_analyze=ValueError("баг в коде"))
    secondary = FakeAIProvider()
    provider = FallbackProvider(primary, secondary)

    with pytest.raises(ValueError, match="баг в коде"):
        await provider.analyze(AnalyzeRequest(system_prompt="x", message_text="привет"))

    assert secondary.analyze_calls == []


async def test_summarize_and_embed_never_use_secondary() -> None:
    """Резерв — только для analyze/generate: остальное не критично для ответа."""
    primary = FakeAIProvider()
    secondary = FakeAIProvider()
    provider = FallbackProvider(primary, secondary)

    from app.ai.provider import SummarizeRequest

    await provider.summarize(SummarizeRequest(messages=[]))
    await provider.embed(["текст"])

    assert secondary.analyze_calls == []
    assert secondary.generate_calls == []
