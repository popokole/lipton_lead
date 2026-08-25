"""ab_variants + conversation.ab_* (A/B заходов)

Revision ID: ll10abtest
Revises: kk09stoplist
Create Date: 2026-08-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ll10abtest"
down_revision: str | None = "kk09stoplist"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ab_variants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("scenario_id", sa.Uuid(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sent_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reply_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["scenario_id"], ["scenarios.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ab_variants")),
    )
    op.create_index("ix_ab_variants_scenario_id", "ab_variants", ["scenario_id"])

    op.add_column("conversations", sa.Column("ab_variant_id", sa.Uuid(), nullable=True))
    op.add_column(
        "conversations",
        sa.Column("ab_reply_counted", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("conversations", "ab_reply_counted")
    op.drop_column("conversations", "ab_variant_id")
    op.drop_index("ix_ab_variants_scenario_id", table_name="ab_variants")
    op.drop_table("ab_variants")
