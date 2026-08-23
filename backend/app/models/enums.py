"""Перечисления доменной модели.

Все они становятся нативными типами PostgreSQL. Значения — то, что реально
лежит в базе, поэтому переименование значения требует миграции.
"""

from __future__ import annotations

from enum import StrEnum


class UserRole(StrEnum):
    ADMIN = "ADMIN"
    OPERATOR = "OPERATOR"
    VIEWER = "VIEWER"


class AccountStatus(StrEnum):
    CREATED = "CREATED"
    AUTHENTICATING = "AUTHENTICATING"
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    ERROR = "ERROR"
    DISABLED = "DISABLED"
    AUTH_REQUIRED = "AUTH_REQUIRED"


class SessionKind(StrEnum):
    STRING = "STRING"
    FILE = "FILE"


class ChatType(StrEnum):
    PRIVATE = "PRIVATE"
    GROUP = "GROUP"
    SUPERGROUP = "SUPERGROUP"
    CHANNEL = "CHANNEL"


class RuleScope(StrEnum):
    """Мониторинг чатов и личные диалоги — разные конвейеры (ТЗ §22)."""

    CHAT_MONITOR = "CHAT_MONITOR"
    DIALOG = "DIALOG"
    ALL = "ALL"  # и личка, и отслеживаемые чаты


class MediaType(StrEnum):
    PHOTO = "PHOTO"
    VIDEO = "VIDEO"
    DOCUMENT = "DOCUMENT"
    AUDIO = "AUDIO"
    VOICE = "VOICE"
    STICKER = "STICKER"
    ANIMATION = "ANIMATION"
    POLL = "POLL"
    LOCATION = "LOCATION"
    CONTACT = "CONTACT"
    WEBPAGE = "WEBPAGE"
    OTHER = "OTHER"


class ProcessedStatus(StrEnum):
    PENDING = "PENDING"
    SKIPPED = "SKIPPED"
    MATCHED = "MATCHED"
    REPLIED = "REPLIED"
    # Правило сработало, ИИ (или cooldown) решил не отвечать — не «в процессе»
    # и не ошибка, отдельный терминальный статус, иначе неотличимо от MATCHED.
    IGNORED = "IGNORED"
    ESCALATED = "ESCALATED"
    # Не-REPLY действие правила выполнено (уведомление, лид, метка).
    ACTED = "ACTED"
    FAILED = "FAILED"


class ConversationStatus(StrEnum):
    NEW = "NEW"
    ACTIVE = "ACTIVE"
    QUALIFIED = "QUALIFIED"
    HOT = "HOT"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    CLOSED = "CLOSED"


class LeadStatus(StrEnum):
    NEW = "NEW"
    COLD = "COLD"
    WARM = "WARM"
    HOT = "HOT"
    CONVERTED = "CONVERTED"
    LOST = "LOST"


class ActionType(StrEnum):
    REPLY = "REPLY"
    NOTIFY_ADMIN = "NOTIFY_ADMIN"
    SAVE_LEAD = "SAVE_LEAD"
    TAG_USER = "TAG_USER"
    ESCALATE_TO_HUMAN = "ESCALATE_TO_HUMAN"
    # Ответ сгенерирован, но ИИ не уверен: отправляем оператору на подтверждение
    # (карточка с кнопками в лог-чате), а не сразу собеседнику.
    REQUEST_REVIEW = "REQUEST_REVIEW"
    IGNORE = "IGNORE"


class ActionStatus(StrEnum):
    PENDING = "PENDING"
    VALIDATING = "VALIDATING"
    READY = "READY"
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class KnowledgeDocumentStatus(StrEnum):
    PENDING = "PENDING"
    PARSING = "PARSING"
    EMBEDDING = "EMBEDDING"
    READY = "READY"
    FAILED = "FAILED"


class WorkerStatus(StrEnum):
    STARTING = "STARTING"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    ERROR = "ERROR"
    STOPPED = "STOPPED"


class AIPurpose(StrEnum):
    ANALYZE = "ANALYZE"
    GENERATE = "GENERATE"
    SUMMARIZE = "SUMMARIZE"
    EMBED = "EMBED"


class EventType(StrEnum):
    """Типы событий структурированного лога (ТЗ §30)."""

    MESSAGE_RECEIVED = "MESSAGE_RECEIVED"
    MESSAGE_SKIPPED = "MESSAGE_SKIPPED"
    RULE_MATCH = "RULE_MATCH"
    AI_ANALYSIS = "AI_ANALYSIS"
    AI_GENERATION = "AI_GENERATION"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    COOLDOWN_BLOCKED = "COOLDOWN_BLOCKED"
    ACTION_CREATED = "ACTION_CREATED"
    ACTION_SENT = "ACTION_SENT"
    ACTION_FAILED = "ACTION_FAILED"
    ACCOUNT_CONNECTED = "ACCOUNT_CONNECTED"
    ACCOUNT_DISCONNECTED = "ACCOUNT_DISCONNECTED"
    ACCOUNT_AUTH_REQUIRED = "ACCOUNT_AUTH_REQUIRED"
    LEAD_UPDATED = "LEAD_UPDATED"
    HUMAN_HANDOFF = "HUMAN_HANDOFF"
    ERROR = "ERROR"


class NotificationType(StrEnum):
    HUMAN_HANDOFF = "HUMAN_HANDOFF"
    ACCOUNT_ERROR = "ACCOUNT_ERROR"
    NEW_LEAD = "NEW_LEAD"
    WORKER_DOWN = "WORKER_DOWN"
    AI_BUDGET = "AI_BUDGET"
