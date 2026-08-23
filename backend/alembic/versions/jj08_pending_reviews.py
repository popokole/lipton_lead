"""pending_reviews + сценарий review_* + notify.review_topic_id

Revision ID: jj08review
Revises: ii07leadcrit
Create Date: 2026-08-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "jj08review"
down_revision: str | None = "ii07leadcrit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pending_reviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("scenario_id", sa.Uuid(), nullable=True),
        sa.Column("rule_id", sa.Uuid(), nullable=True),
        sa.Column("chat_id", sa.Uuid(), nullable=True),
        sa.Column("tg_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("reply_to_tg_message_id", sa.BigInteger(), nullable=True),
        sa.Column("target_sender_tg_id", sa.BigInteger(), nullable=True),
        sa.Column("reply_text", sa.Text(), nullable=False),
        sa.Column("dm_text", sa.Text(), nullable=True),
        sa.Column("incoming_text", sa.Text(), nullable=True),
        sa.Column("sender_username", sa.String(length=64), nullable=True),
        sa.Column("sender_display_name", sa.String(length=255), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=3, scale=2), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("notify_message_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["scenario_id"], ["scenarios.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["rule_id"], ["rules.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["chat_id"], ["chats.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pending_reviews")),
    )
    op.create_index("ix_pending_reviews_status", "pending_reviews", ["status"])
    op.create_index(
        "ix_pending_reviews_created_at", "pending_reviews", [sa.text("created_at DESC")]
    )

    op.add_column(
        "scenarios",
        sa.Column(
            "review_when_uncertain",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "scenarios",
        sa.Column("review_min_confidence", sa.Numeric(precision=3, scale=2), nullable=True),
    )
    op.add_column("notify_settings", sa.Column("review_topic_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("notify_settings", "review_topic_id")
    op.drop_column("scenarios", "review_min_confidence")
    op.drop_column("scenarios", "review_when_uncertain")
    op.drop_index("ix_pending_reviews_created_at", table_name="pending_reviews")
    op.drop_index("ix_pending_reviews_status", table_name="pending_reviews")
    op.drop_table("pending_reviews")
