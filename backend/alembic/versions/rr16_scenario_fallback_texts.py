"""scenarios.fallback_text -> fallback_texts (пул вариантов)

Revision ID: rr16fallback
Revises: qq15topics
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "rr16fallback"
down_revision: str | None = "qq15topics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "scenarios",
        sa.Column(
            "fallback_texts",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.execute(
        """
        UPDATE scenarios SET fallback_texts = jsonb_build_array(fallback_text)
        WHERE fallback_text IS NOT NULL AND fallback_text <> ''
        """
    )
    op.drop_column("scenarios", "fallback_text")


def downgrade() -> None:
    op.add_column("scenarios", sa.Column("fallback_text", sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE scenarios SET fallback_text = fallback_texts->>0
        WHERE jsonb_array_length(fallback_texts) > 0
        """
    )
    op.drop_column("scenarios", "fallback_texts")
