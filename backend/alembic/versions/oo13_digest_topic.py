"""notify_settings.digest_topic_id

Revision ID: oo13digest
Revises: nn12oneshot
Create Date: 2026-08-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "oo13digest"
down_revision: str | None = "nn12oneshot"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("notify_settings", sa.Column("digest_topic_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("notify_settings", "digest_topic_id")
