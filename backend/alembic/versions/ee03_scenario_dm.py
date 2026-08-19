"""scenario reply_in_dm and group_ack_text

Revision ID: ee03scendm
Revises: dd02msgstatus
Create Date: 2026-08-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ee03scendm"
down_revision: str | None = "dd02msgstatus"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "scenarios",
        sa.Column("reply_in_dm", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("scenarios", sa.Column("group_ack_text", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("scenarios", "group_ack_text")
    op.drop_column("scenarios", "reply_in_dm")
