"""Ограничение и учёт расходов на AI.

Без этого система тратит деньги молча. Здесь три механизма:

* семафор — сколько обращений к модели идёт одновременно. Защищает и от
  превышения лимитов поставщика, и от ситуации, когда всплеск сообщений
  превращается в сотню параллельных запросов;
* дневной бюджет — счётчик в Redis. При исчерпании обработка не падает, а
  деградирует: сообщение уходит человеку вместо модели;
* журнал `ai_requests` — токены, деньги и задержка каждого обращения.

Стоимость считается по ценам из настроек. Если цены не заданы, деньги не
считаются, а токены учитываются всё равно: выдумывать прайс-лист в коде нельзя,
он устаревает молча.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.provider import Usage
from app.bus import keys
from app.core.clock import utcnow
from app.core.config import Settings
from app.core.errors import AIBudgetExceededError
from app.core.logging import get_logger
from app.models import AIPurpose, AIRequest

logger = get_logger(__name__)

# Счётчик расходов живёт двое суток: за отчётами ходят в PostgreSQL, в Redis
# он нужен лишь для проверки текущего дневного лимита.
BUDGET_TTL_SECONDS = 172_800


def estimate_cost(usage: Usage, settings: Settings) -> Decimal:
    if not settings.ai_price_input_per_1m_usd and not settings.ai_price_output_per_1m_usd:
        return Decimal("0")
    million = Decimal(1_000_000)
    return (
        Decimal(usage.prompt_tokens) * Decimal(str(settings.ai_price_input_per_1m_usd)) / million
        + Decimal(usage.completion_tokens)
        * Decimal(str(settings.ai_price_output_per_1m_usd))
        / million
    ).quantize(Decimal("0.000001"))


class AIBudget:
    """Дневной лимит расходов и ограничение одновременных обращений."""

    def __init__(self, settings: Settings, redis: Redis) -> None:
        self._settings = settings
        self._redis = redis
        self._semaphore = asyncio.Semaphore(settings.ai_max_concurrency)

    @property
    def limit_usd(self) -> float:
        return self._settings.ai_daily_budget_usd

    @staticmethod
    def _today_key() -> str:
        return keys.ai_budget(utcnow().strftime("%Y-%m-%d"))

    async def spent_today(self) -> Decimal:
        raw = await self._redis.get(self._today_key())
        return Decimal(str(raw)) if raw else Decimal("0")

    async def ensure_available(self) -> None:
        """Бросает AIBudgetExceededError, если дневной лимит исчерпан."""
        if self.limit_usd <= 0:
            return
        if await self.spent_today() >= Decimal(str(self.limit_usd)):
            raise AIBudgetExceededError(f"Дневной бюджет AI исчерпан ({self.limit_usd:.2f} USD)")

    async def charge(self, cost: Decimal) -> None:
        if cost <= 0:
            return
        key = self._today_key()
        # incrbyfloat, а не get+set: два воркера считают в один счётчик.
        await self._redis.incrbyfloat(key, float(cost))
        await self._redis.expire(key, BUDGET_TTL_SECONDS)

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Занимает место в очереди обращений к модели."""
        await self.ensure_available()
        async with self._semaphore:
            yield


class UsageRecorder:
    """Пишет расход в PostgreSQL — источник истины для отчётов."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def record(
        self,
        db: AsyncSession,
        *,
        provider: str,
        purpose: AIPurpose,
        usage: Usage,
        account_id: uuid.UUID | None = None,
        message_id: uuid.UUID | None = None,
        scenario_id: uuid.UUID | None = None,
        status: str = "OK",
        error: str | None = None,
    ) -> Decimal:
        cost = estimate_cost(usage, self._settings)
        db.add(
            AIRequest(
                provider=provider,
                model=usage.model or self._settings.default_ai_model,
                purpose=purpose,
                account_id=account_id,
                message_id=message_id,
                scenario_id=scenario_id,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                cost_usd=cost,
                latency_ms=usage.latency_ms or None,
                status=status,
                error=error,
            )
        )
        return cost
