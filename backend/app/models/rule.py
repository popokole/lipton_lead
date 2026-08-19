"""Правила обработки сообщений (ТЗ §8).

Условия правила разложены на две части. Всё, что участвует в выборке правил
из базы (аккаунты и чаты), вынесено в связующие таблицы — по ним есть индексы.
Всё, что проверяется в памяти уже над коротким списком кандидатов (ключевые
слова, regex, типы сообщений), лежит в JSONB: эти условия слишком разнородны,
чтобы раскладывать их по колонкам.
"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models._types import pg_enum
from app.models.enums import ActionType, RuleScope


class Rule(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "rules"

    name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    # Правила проверяются по убыванию priority; stop_on_match завершает подбор
    # на первом совпадении — иначе одно сообщение породило бы несколько ответов.
    priority: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=100)
    stop_on_match: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)

    scope: Mapped[RuleScope] = mapped_column(
        pg_enum(RuleScope, "rule_scope"), nullable=False, default=RuleScope.CHAT_MONITOR
    )
    scenario_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("scenarios.id", ondelete="SET NULL")
    )

    # {"incoming_only": true, "chat_types": [...], "forwarded": false,
    #  "text_only": true, "sender_ids": [...], "exclude_sender_ids": [...],
    #  "languages": ["ru"]}
    filters: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb")
    )
    # {"terms": [...], "exclude": [...], "match": "substring|whole_word|exact",
    #  "case_sensitive": false}
    keywords: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb")
    )
    regex: Mapped[str | None] = mapped_column(sa.Text)

    ai_enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    ai_threshold: Mapped[float | None] = mapped_column(sa.Numeric(3, 2))

    # {"user": 600, "chat": 60, "account": 10, "rule": 0, "scenario": 0} — секунды.
    cooldown: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb")
    )

    action: Mapped[ActionType] = mapped_column(
        pg_enum(ActionType, "action_type"), nullable=False, default=ActionType.REPLY
    )
    action_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb")
    )

    __table_args__ = (
        sa.CheckConstraint(
            "ai_threshold IS NULL OR (ai_threshold >= 0 AND ai_threshold <= 1)",
            name="ai_threshold_range",
        ),
        sa.CheckConstraint(
            "NOT ai_enabled OR ai_threshold IS NOT NULL", name="ai_threshold_required"
        ),
        sa.Index("ix_rules_enabled_priority", "enabled", sa.desc("priority")),
        sa.Index("ix_rules_scope", "scope"),
    )


class RuleAccount(Base):
    """К каким аккаунтам применяется правило. Пусто — значит ко всем."""

    __tablename__ = "rule_accounts"

    rule_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("rules.id", ondelete="CASCADE"), primary_key=True
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )

    __table_args__ = (sa.Index("ix_rule_accounts_account_id", "account_id"),)


class RuleChat(Base):
    """В каких чатах работает правило. Пусто — значит во всех отслеживаемых."""

    __tablename__ = "rule_chats"

    rule_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("rules.id", ondelete="CASCADE"), primary_key=True
    )
    chat_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), sa.ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True
    )

    __table_args__ = (sa.Index("ix_rule_chats_chat_id", "chat_id"),)
