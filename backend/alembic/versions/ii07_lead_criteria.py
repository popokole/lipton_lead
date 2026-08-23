"""scenario.lead_criteria — критерий отбора лида для анализатора

Revision ID: ii07leadcrit
Revises: hh06streams
Create Date: 2026-08-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ii07leadcrit"
down_revision: str | None = "hh06streams"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("scenarios", sa.Column("lead_criteria", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("scenarios", "lead_criteria")
