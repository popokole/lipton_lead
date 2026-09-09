"""story_settings — авто-просмотр историй (прогрев)

Revision ID: uu19story
Revises: tt18personabase
Create Date: 2026-09-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "uu19story"
down_revision: str | None = "tt18personabase"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "story_settings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("per_hour", sa.Integer(), server_default="20", nullable=False),
        sa.Column("active_window_hours", sa.Integer(), server_default="48", nullable=False),
        sa.Column("per_user_cooldown_hours", sa.Integer(), server_default="20", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_story_settings")),
    )


def downgrade() -> None:
    op.drop_table("story_settings")
