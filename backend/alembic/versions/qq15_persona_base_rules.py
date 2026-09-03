"""persona: глобальный базовый промпт и общая макс. длина ответа

Revision ID: qq15personabase
Revises: pp14testmode
Create Date: 2026-09-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "qq15personabase"
down_revision: str | None = "pp14testmode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("persona", sa.Column("base_rules", sa.Text(), nullable=True))
    op.add_column("persona", sa.Column("max_reply_length", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("persona", "max_reply_length")
    op.drop_column("persona", "base_rules")
