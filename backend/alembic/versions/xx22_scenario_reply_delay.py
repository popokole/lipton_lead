"""scenario reply delay — своя задержка перед авто-ответом на сценарий

Revision ID: xx22delay
Revises: vv20react
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "xx22delay"
down_revision: str | None = "vv20react"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("scenarios", sa.Column("reply_delay_min_seconds", sa.Integer(), nullable=True))
    op.add_column("scenarios", sa.Column("reply_delay_max_seconds", sa.Integer(), nullable=True))
    op.create_check_constraint(
        op.f("ck_scenarios_reply_delay_range"),
        "scenarios",
        "(reply_delay_min_seconds IS NULL OR "
        "(reply_delay_min_seconds >= 0 AND reply_delay_min_seconds <= 3600)) AND "
        "(reply_delay_max_seconds IS NULL OR "
        "(reply_delay_max_seconds >= 0 AND reply_delay_max_seconds <= 3600))",
    )
    op.create_check_constraint(
        op.f("ck_scenarios_reply_delay_order"),
        "scenarios",
        "reply_delay_min_seconds IS NULL OR reply_delay_max_seconds IS NULL "
        "OR reply_delay_min_seconds <= reply_delay_max_seconds",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_scenarios_reply_delay_order"), "scenarios", type_="check")
    op.drop_constraint(op.f("ck_scenarios_reply_delay_range"), "scenarios", type_="check")
    op.drop_column("scenarios", "reply_delay_max_seconds")
    op.drop_column("scenarios", "reply_delay_min_seconds")
