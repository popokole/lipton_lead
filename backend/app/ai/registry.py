"""Выбор поставщика AI по настройкам (ТЗ §12).

Единственное место, где имя из конфигурации превращается в реализацию. Ни
конвейер, ни правила не знают, какой поставщик используется.
"""

from __future__ import annotations

from app.ai.provider import AIProvider
from app.core.config import Settings
from app.core.errors import AIError

PROVIDERS = ("openai",)


def build_provider(settings: Settings) -> AIProvider:
    name = settings.ai_provider.strip().lower()

    if name == "openai":
        from app.ai.openai_provider import OpenAIProvider

        return OpenAIProvider(settings)

    raise AIError(
        f"Неизвестный AI_PROVIDER: {settings.ai_provider!r}. Доступные: {', '.join(PROVIDERS)}"
    )


def provider_is_configured(settings: Settings) -> bool:
    """Готов ли поставщик к работе.

    Нужно API и воркеру: без ключа система должна работать в режиме
    мониторинга без AI, а не падать на старте.
    """
    if settings.ai_provider.strip().lower() == "openai":
        return settings.openai_api_key is not None
    return False
