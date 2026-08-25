"""Схемы ресурсов панели.

Общее правило: наружу не отдаётся ничего, что связано с сессией Telegram.
В `AccountOut` полей сессии нет физически — не «скрыты фильтром», а просто
отсутствуют, поэтому утечь оттуда нечему.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models import (
    AccountStatus,
    ActionStatus,
    ActionType,
    ChatType,
    ConversationStatus,
    EventType,
    LeadStatus,
    ProcessedStatus,
    RuleScope,
    WorkerStatus,
)
from app.schemas.common import ORMModel


# --- аккаунты --------------------------------------------------------------
class AccountOut(ORMModel):
    id: uuid.UUID
    label: str
    tg_user_id: int | None
    username: str | None
    display_name: str | None
    status: AccountStatus
    enabled: bool
    proxy_id: uuid.UUID | None
    worker_id: uuid.UUID | None
    last_seen_at: datetime | None
    last_error: str | None
    created_at: datetime


class AccountCreate(BaseModel):
    label: str = Field(min_length=1, max_length=120)


class AccountUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=120)
    enabled: bool | None = None
    # None здесь означает «не менять»; чтобы отвязать прокси, передайте
    # detach_proxy = true — иначе значение null было бы неотличимо от «пропустить».
    proxy_id: uuid.UUID | None = None
    detach_proxy: bool = False


class ProxyOut(ORMModel):
    id: uuid.UUID
    name: str
    scheme: str
    host: str
    port: int
    username: str | None
    enabled: bool


class ProxyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    scheme: str = Field(default="socks5", pattern="^(socks5|socks4|http|mtproxy)$")
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    username: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, max_length=255)
    enabled: bool = True


class SendCodeRequest(BaseModel):
    phone: str = Field(min_length=5, max_length=20)
    # Собственные credentials с my.telegram.org. Их принимает именно этот шаг:
    # раньше их негде хранить (сессии ещё нет), а чужие api_id использовать
    # нельзя — это нарушение условий Telegram и причина блокировки аккаунта.
    # Если не переданы, берутся общие из TELEGRAM_API_ID/TELEGRAM_API_HASH.
    api_id: int | None = None
    api_hash: str | None = Field(default=None, max_length=64)


class SignInRequest(BaseModel):
    code: str = Field(min_length=3, max_length=16)


class SignInPasswordRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class AuthStepResult(BaseModel):
    ok: bool
    password_required: bool = False
    authorized: bool = False
    detail: str | None = None


class TDataAccountOut(BaseModel):
    """Аккаунт, найденный внутри папки tdata."""

    index: int
    tg_user_id: int
    dc_id: int


class DialogOut(BaseModel):
    tg_chat_id: int
    title: str | None
    username: str | None
    is_group: bool
    is_channel: bool
    is_user: bool


# --- чаты ------------------------------------------------------------------
class ChatOut(ORMModel):
    id: uuid.UUID
    account_id: uuid.UUID
    tg_chat_id: int
    type: ChatType
    title: str | None
    username: str | None
    monitored: bool
    last_message_at: datetime | None
    has_avatar: bool = False

    @classmethod
    def from_orm_chat(cls, chat: Any) -> ChatOut:
        data = cls.model_validate(chat)
        return data.model_copy(update={"has_avatar": chat.avatar is not None})


class ChatNode(BaseModel):
    """Узел дерева чатов со счётчиками активности."""

    id: uuid.UUID
    tg_chat_id: int
    type: ChatType
    title: str | None
    username: str | None
    monitored: bool
    has_avatar: bool
    last_message_at: datetime | None
    messages_total: int
    leads_count: int
    replies_count: int
    active_users: int


class ChatTreeAccount(BaseModel):
    account_id: uuid.UUID
    label: str
    username: str | None
    messages_total: int
    leads_count: int
    chats: list[ChatNode]


class ChatCreate(BaseModel):
    account_id: uuid.UUID
    tg_chat_id: int
    type: ChatType = ChatType.SUPERGROUP
    title: str | None = Field(default=None, max_length=255)
    username: str | None = Field(default=None, max_length=64)
    monitored: bool = True


class ChatUpdate(BaseModel):
    monitored: bool | None = None
    title: str | None = Field(default=None, max_length=255)


# --- сценарии --------------------------------------------------------------
class ScenarioOut(ORMModel):
    id: uuid.UUID
    name: str
    description: str | None
    system_prompt: str
    lead_criteria: str | None
    model: str | None
    temperature: float | None
    max_tokens: int | None
    language: str | None
    tone: str | None
    max_reply_length: int | None
    context_messages: int | None
    knowledge_base_id: uuid.UUID | None
    require_knowledge_grounding: bool
    fallback_text: str | None
    human_handoff_enabled: bool
    reply_in_dm: bool
    group_ack_text: str | None
    min_confidence: float | None
    review_when_uncertain: bool
    review_min_confidence: float | None
    enabled: bool


class ScenarioCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    system_prompt: str = Field(min_length=1)
    lead_criteria: str | None = Field(default=None, max_length=4000)
    description: str | None = None
    model: str | None = Field(default=None, max_length=120)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1)
    language: str | None = Field(default=None, max_length=8)
    tone: str | None = Field(default=None, max_length=64)
    max_reply_length: int | None = Field(default=None, ge=1)
    context_messages: int | None = Field(default=None, ge=1)
    knowledge_base_id: uuid.UUID | None = None
    require_knowledge_grounding: bool = False
    fallback_text: str | None = None
    human_handoff_enabled: bool = True
    reply_in_dm: bool = False
    group_ack_text: str | None = None
    min_confidence: float | None = Field(default=None, ge=0, le=1)
    review_when_uncertain: bool = False
    review_min_confidence: float | None = Field(default=None, ge=0, le=1)
    enabled: bool = True


class ScenarioUpdate(ScenarioCreate):
    name: str | None = Field(default=None, min_length=1, max_length=120)  # type: ignore[assignment]
    system_prompt: str | None = Field(default=None, min_length=1)  # type: ignore[assignment]


# --- правила ---------------------------------------------------------------
class RuleOut(ORMModel):
    id: uuid.UUID
    name: str
    description: str | None
    enabled: bool
    priority: int
    stop_on_match: bool
    scope: RuleScope
    scenario_id: uuid.UUID | None
    filters: dict[str, Any]
    keywords: dict[str, Any]
    regex: str | None
    ai_enabled: bool
    ai_threshold: float | None
    cooldown: dict[str, Any]
    action: ActionType
    action_config: dict[str, Any]


class RuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = None
    enabled: bool = True
    priority: int = Field(default=100, ge=0, le=10_000)
    stop_on_match: bool = True
    scope: RuleScope = RuleScope.CHAT_MONITOR
    scenario_id: uuid.UUID | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    keywords: dict[str, Any] = Field(default_factory=dict)
    regex: str | None = None
    ai_enabled: bool = False
    ai_threshold: float | None = Field(default=None, ge=0, le=1)
    cooldown: dict[str, Any] = Field(default_factory=dict)
    action: ActionType = ActionType.REPLY
    action_config: dict[str, Any] = Field(default_factory=dict)
    account_ids: list[uuid.UUID] = Field(default_factory=list)
    chat_ids: list[uuid.UUID] = Field(default_factory=list)


class RuleUpdate(RuleCreate):
    name: str | None = Field(default=None, min_length=1, max_length=120)  # type: ignore[assignment]


# --- сообщения и журналы ---------------------------------------------------
class MessageOut(ORMModel):
    id: uuid.UUID
    account_id: uuid.UUID
    chat_id: uuid.UUID | None
    chat_title: str | None = None
    chat_username: str | None = None
    chat_has_avatar: bool = False
    tg_chat_id: int
    tg_message_id: int
    sender_tg_id: int | None
    sender_username: str | None
    sender_display_name: str | None
    text: str | None
    date: datetime
    is_incoming: bool
    is_bot_reply: bool
    processed_status: ProcessedStatus
    status_reason: str | None = None
    rule_id: uuid.UUID | None


class ActionOut(ORMModel):
    id: uuid.UUID
    type: ActionType
    status: ActionStatus
    account_id: uuid.UUID
    chat_id: uuid.UUID | None
    rule_id: uuid.UUID | None
    reply_text: str | None
    validation: dict[str, Any] | None
    error: str | None
    sent_at: datetime | None
    created_at: datetime


class EscalationOut(BaseModel):
    """Диалог, переданный оператору (ТЗ §21) — для экрана «Требует внимания»."""

    conversation_id: uuid.UUID
    account_id: uuid.UUID
    account_label: str | None
    peer_tg_id: int
    chat_id: uuid.UUID | None
    chat_title: str | None
    chat_username: str | None
    chat_has_avatar: bool = False
    status: ConversationStatus
    reason: str | None
    suggested_reply: str | None
    notification_id: uuid.UUID | None
    summary: str | None
    last_message_at: datetime | None
    created_at: datetime


class EventLogOut(ORMModel):
    id: int
    ts: datetime
    level: str
    event_type: EventType
    account_id: uuid.UUID | None
    chat_id: uuid.UUID | None
    chat_title: str | None = None
    chat_username: str | None = None
    chat_has_avatar: bool = False
    message_id: uuid.UUID | None
    rule_id: uuid.UUID | None
    duration_ms: int | None
    status: str | None
    error: str | None
    extra: dict[str, Any] | None


class ConversationOut(ORMModel):
    id: uuid.UUID
    account_id: uuid.UUID
    peer_tg_id: int
    status: ConversationStatus
    summary: str | None
    message_count: int
    ai_replies_in_row: int
    last_message_at: datetime | None


class LeadOut(ORMModel):
    id: uuid.UUID
    account_id: uuid.UUID
    tg_user_id: int
    username: str | None
    display_name: str | None
    intent: str | None
    score: int
    status: LeadStatus
    first_seen_at: datetime
    last_seen_at: datetime | None
    notes: str | None


class ThreadOut(BaseModel):
    """Строка списка «Общение»: один чат (личка или группа) с превью реплики.

    Тред привязан к чату, а не к человеку: личные диалоги и группы не
    смешиваются. Для личек, где собеседник — лид, добавляются балл и статус.
    """

    chat_id: uuid.UUID
    account_id: uuid.UUID
    tg_chat_id: int
    # 'dm' — личный диалог, 'group' — группа/супергруппа.
    kind: str
    title: str | None
    username: str | None
    has_avatar: bool
    monitored: bool
    message_count: int
    last_message_at: datetime | None
    last_text: str | None
    # Последнее сообщение — входящее: значит ждёт нашего ответа.
    awaiting_reply: bool
    lead_score: int | None = None
    lead_status: LeadStatus | None = None
    # Групповой чат: включён ли режим «общение ИИ» (бот отвечает по решению ИИ).
    ai_chat: bool = False


# --- воркеры и сводка ------------------------------------------------------
class WorkerOut(BaseModel):
    id: str
    name: str
    status: WorkerStatus | str
    hostname: str | None
    pid: int | None
    version: str | None
    accounts: list[str] = Field(default_factory=list)
    accounts_count: int = 0
    queue_size: int = 0
    last_error: str | None = None
    updated_at: str | None = None
    alive: bool = True


class DashboardCounters(BaseModel):
    accounts_total: int
    accounts_online: int
    chats_monitored: int
    messages_today: int
    ai_analyzed_today: int
    replies_today: int
    leads_total: int
    errors_today: int
    workers_healthy: int


class DailyPoint(BaseModel):
    day: str
    value: int


class DashboardSeries(BaseModel):
    messages: list[DailyPoint]
    matches: list[DailyPoint]
    replies: list[DailyPoint]
    leads: list[DailyPoint]
    errors: list[DailyPoint]


class RuleStatOut(BaseModel):
    rule_id: uuid.UUID
    rule_name: str
    enabled: bool
    matches: int
    replies: int
