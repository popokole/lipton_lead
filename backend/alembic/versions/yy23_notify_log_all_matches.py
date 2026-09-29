"""notify_settings.log_all_matches — карточка в лог-чат на каждое совпадение

Revision ID: yy23matches
Revises: xx22delay
Create Date: 2026-09-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "yy23matches"
down_revision: str | None = "xx22delay"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # По умолчанию включено: владелец хочет видеть в лог-чате все совпадения.
    op.add_column(
        "notify_settings",
        sa.Column("log_all_matches", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("notify_settings", "log_all_matches")
