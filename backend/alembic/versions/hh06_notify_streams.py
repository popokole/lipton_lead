"""notify: постоянные топики-ленты (личка/группы)

Revision ID: hh06streams
Revises: gg05persona
Create Date: 2026-08-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "hh06streams"
down_revision: str | None = "gg05persona"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("notify_settings", sa.Column("dm_topic_id", sa.BigInteger(), nullable=True))
    op.add_column("notify_settings", sa.Column("group_topic_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("notify_settings", "group_topic_id")
    op.drop_column("notify_settings", "dm_topic_id")
