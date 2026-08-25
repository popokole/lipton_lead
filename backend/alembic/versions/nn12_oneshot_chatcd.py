"""scenario.one_shot + chats.cooldown_exempt

Revision ID: nn12oneshot
Revises: mm11kbfts
Create Date: 2026-08-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "nn12oneshot"
down_revision: str | None = "mm11kbfts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "scenarios",
        sa.Column("one_shot", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "chats",
        sa.Column("cooldown_exempt", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("chats", "cooldown_exempt")
    op.drop_column("scenarios", "one_shot")
