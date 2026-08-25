"""stoplist_entries — кому никогда не отвечаем

Revision ID: kk09stoplist
Revises: jj08review
Create Date: 2026-08-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "kk09stoplist"
down_revision: str | None = "jj08review"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stoplist_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tg_user_id", sa.BigInteger(), nullable=True),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stoplist_entries")),
    )
    op.create_index("ix_stoplist_tg_user_id", "stoplist_entries", ["tg_user_id"])
    op.create_index("ix_stoplist_username", "stoplist_entries", ["username"])


def downgrade() -> None:
    op.drop_index("ix_stoplist_username", table_name="stoplist_entries")
    op.drop_index("ix_stoplist_tg_user_id", table_name="stoplist_entries")
    op.drop_table("stoplist_entries")
