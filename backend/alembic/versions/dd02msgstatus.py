"""processed_status: IGNORED/ESCALATED/ACTED + status_reason column

Revision ID: dd02msgstatus
Revises: cc01ruleall
Create Date: 2026-08-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "dd02msgstatus"
down_revision: str | None = "cc01ruleall"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Новые значения нативного enum добавляются вне транзакции.
    op.execute("COMMIT")
    op.execute("ALTER TYPE processed_status ADD VALUE IF NOT EXISTS 'IGNORED'")
    op.execute("ALTER TYPE processed_status ADD VALUE IF NOT EXISTS 'ESCALATED'")
    op.execute("ALTER TYPE processed_status ADD VALUE IF NOT EXISTS 'ACTED'")
    op.add_column("messages", sa.Column("status_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    # Удалить значения из enum PostgreSQL нельзя без пересоздания типа;
    # оставляем их в типе — данные это не ломает.
    op.drop_column("messages", "status_reason")
