"""notify settings + scenario notify_topic_id

Revision ID: ff04notify
Revises: ee03scendm
Create Date: 2026-08-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ff04notify"
down_revision: str | None = "ee03scendm"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notify_settings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("bot_token_ct", sa.LargeBinary(), nullable=True),
        sa.Column("bot_token_nonce", sa.LargeBinary(), nullable=True),
        sa.Column("bot_token_key_id", sa.String(length=32), nullable=True),
        sa.Column("group_id", sa.BigInteger(), nullable=True),
        sa.Column("bot_username", sa.String(length=64), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notify_settings")),
    )
    op.add_column("scenarios", sa.Column("notify_topic_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("scenarios", "notify_topic_id")
    op.drop_table("notify_settings")
