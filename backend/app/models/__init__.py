"""Доменные модели.

Импорт этого пакета регистрирует все таблицы в Base.metadata — именно на него
опирается autogenerate в alembic/env.py.
"""

from __future__ import annotations

from app.models.account import Account, Proxy, TelegramSession
from app.models.action import Action, ActionLog
from app.models.chat import Chat
from app.models.conversation import Conversation, UserMemory
from app.models.enums import (
    AccountStatus,
    ActionStatus,
    ActionType,
    AIPurpose,
    ChatType,
    ConversationStatus,
    EventType,
    KnowledgeDocumentStatus,
    LeadStatus,
    MediaType,
    NotificationType,
    ProcessedStatus,
    RuleScope,
    SessionKind,
    UserRole,
    WorkerStatus,
)
from app.models.knowledge import (
    EMBEDDING_DIM,
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
)
from app.models.lead import Lead
from app.models.message import Message, ProcessedMessage
from app.models.observability import AIRequest, AuditLog, EventLog, Notification, Worker
from app.models.rule import Rule, RuleAccount, RuleChat
from app.models.scenario import Scenario
from app.models.user import User

__all__ = [
    "EMBEDDING_DIM",
    "AIPurpose",
    "AIRequest",
    "Account",
    "AccountStatus",
    "Action",
    "ActionLog",
    "ActionStatus",
    "ActionType",
    "AuditLog",
    "Chat",
    "ChatType",
    "Conversation",
    "ConversationStatus",
    "EventLog",
    "EventType",
    "KnowledgeBase",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "KnowledgeDocumentStatus",
    "Lead",
    "LeadStatus",
    "MediaType",
    "Message",
    "Notification",
    "NotificationType",
    "ProcessedMessage",
    "ProcessedStatus",
    "Proxy",
    "Rule",
    "RuleAccount",
    "RuleChat",
    "RuleScope",
    "Scenario",
    "SessionKind",
    "TelegramSession",
    "User",
    "UserMemory",
    "UserRole",
    "Worker",
    "WorkerStatus",
]
