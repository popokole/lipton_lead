"""add ALL to rule_scope enum

Revision ID: cc01ruleall
Revises: bbfbc9b62d8c
Create Date: 2026-08-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "cc01ruleall"
down_revision: str | None = "bbfbc9b62d8c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Новое значение нативного enum добавляется вне транзакции.
    op.execute("COMMIT")
    op.execute("ALTER TYPE rule_scope ADD VALUE IF NOT EXISTS 'ALL'")


def downgrade() -> None:
    # Удалить значение из enum PostgreSQL нельзя без пересоздания типа;
    # оставляем 'ALL' в типе — данные это не ломает.
    pass
