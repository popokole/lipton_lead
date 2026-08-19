"""Интерфейс поставщика AI и структуры данных (ТЗ §11, §12).

Бизнес-логика обращается только к этому интерфейсу. Смена поставщика —
добавление ещё одной реализации, а не правка конвейера.

Результаты моделей описаны схемами Pydantic намеренно: полагаться на свободный
текст нельзя. Модель может ответить «пожалуй, да» вместо булева значения, и
тогда решение «отвечать или нет» будет приниматься по разбору строки. Схема
превращает такой ответ в явную ошибку, которую видно в журнале.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Intent(StrEnum):
    """Зачем человек написал. Список закрытый: открытый набор невозможно
    использовать в правилах и статистике."""

    SERVICE_REQUEST = "service_request"
    PRICE_REQUEST = "price_request"
    SUPPORT = "support"
    COMPLAINT = "complaint"
    SMALL_TALK = "small_talk"
    SPAM = "spam"
    JOB_OFFER = "job_offer"
    JOB_SEARCH = "job_search"
    HUMAN_REQUEST = "human_request"
    OTHER = "other"


class AnalysisResult(BaseModel):
    """Структурированный вывод анализатора (ТЗ §11)."""

    model_config = ConfigDict(extra="ignore")

    relevant: bool
    confidence: float = Field(ge=0.0, le=1.0)
    intent: Intent = Intent.OTHER
    should_reply: bool = False
    needs_human: bool = False
    lead_score: int = Field(default=0, ge=0, le=100)
    reason: str = ""

    @field_validator("intent", mode="before")
    @classmethod
    def _tolerant_intent(cls, value: object) -> object:
        # Модель любит писать intent свободным текстом («запрос на психолога»).
        # Неизвестное значение — не повод ронять весь анализ: считаем OTHER.
        if isinstance(value, Intent):
            return value
        if isinstance(value, str):
            key = value.strip().lower()
            return key if key in {item.value for item in Intent} else Intent.OTHER
        return Intent.OTHER

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, value: object) -> object:
        try:
            return min(1.0, max(0.0, float(value)))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0

    @field_validator("lead_score", mode="before")
    @classmethod
    def _clamp_score(cls, value: object) -> object:
        try:
            return min(100, max(0, int(float(value))))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0

    @field_validator("reason")
    @classmethod
    def _trim_reason(cls, value: str) -> str:
        return value.strip()[:500]

    @property
    def is_actionable(self) -> bool:
        return self.relevant and self.should_reply and not self.needs_human


class GeneratedReply(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str
    # Короткая фраза в группу при режиме «в чат + в личку»: живая, разная
    # каждый раз. Пусто — используется запасная фраза сценария.
    group_text: str = ""
    used_knowledge: bool = False
    # Модель сообщает, что не может ответить по имеющимся данным. Это честнее,
    # чем выдумать ответ, и приводит к передаче диалога человеку.
    refused: bool = False
    refusal_reason: str = ""


class Summary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str
    facts: list[str] = Field(default_factory=list)


class ChatMessage(BaseModel):
    """Одна реплика в промпте."""

    role: str
    content: str


class AnalyzeRequest(BaseModel):
    system_prompt: str
    message_text: str
    context: list[ChatMessage] = Field(default_factory=list)
    rule_name: str | None = None
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None


class GenerateRequest(BaseModel):
    system_prompt: str
    message_text: str
    context: list[ChatMessage] = Field(default_factory=list)
    knowledge: list[str] = Field(default_factory=list)
    memory: dict[str, str] = Field(default_factory=dict)
    conversation_summary: str | None = None
    require_grounding: bool = False
    reply_in_dm: bool = False
    max_reply_length: int | None = None
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None


class SummarizeRequest(BaseModel):
    messages: list[ChatMessage]
    previous_summary: str | None = None
    language: str | None = None
    model: str | None = None


class Usage(BaseModel):
    """Расход одного обращения — основа учёта стоимости (ТЗ §12)."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    latency_ms: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class AIResponse[T](BaseModel):
    """Результат вместе с расходом: одно без другого учитывать нельзя."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    result: T
    usage: Usage


class AIProvider(Protocol):
    """Контракт поставщика (ТЗ §12)."""

    @property
    def name(self) -> str: ...

    async def analyze(self, request: AnalyzeRequest) -> AIResponse[AnalysisResult]: ...

    async def generate(self, request: GenerateRequest) -> AIResponse[GeneratedReply]: ...

    async def summarize(self, request: SummarizeRequest) -> AIResponse[Summary]: ...

    async def embed(
        self, texts: list[str], model: str | None = None
    ) -> AIResponse[list[list[float]]]: ...

    async def close(self) -> None: ...


def json_schema_of(model: type[BaseModel]) -> dict[str, Any]:
    """Схема для structured output поставщика.

    OpenAI требует `additionalProperties: false` и перечисления всех полей в
    `required`, иначе запрос отклоняется.
    """
    schema = model.model_json_schema()
    _tighten(schema)
    return schema


def _tighten(node: Any) -> None:
    if isinstance(node, dict):
        if node.get("type") == "object":
            node["additionalProperties"] = False
            properties = node.get("properties")
            if isinstance(properties, dict):
                node["required"] = list(properties)
        for value in node.values():
            _tighten(value)
    elif isinstance(node, list):
        for item in node:
            _tighten(item)
