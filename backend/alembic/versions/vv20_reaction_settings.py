"""reaction_settings — авто-реакции на посты (прогрев)

Revision ID: vv20react
Revises: uu19story
Create Date: 2026-09-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "vv20react"
down_revision: str | None = "uu19story"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reaction_settings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("per_hour", sa.Integer(), server_default="15", nullable=False),
        sa.Column("fresh_window_hours", sa.Integer(), server_default="6", nullable=False),
        sa.Column("per_chat_cooldown_minutes", sa.Integer(), server_default="30", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reaction_settings")),
    )


def downgrade() -> None:
    op.drop_table("reaction_settings")
