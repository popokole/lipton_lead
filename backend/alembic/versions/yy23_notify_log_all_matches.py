"""notify_settings.log_all_matches — карточка в лог-чат на каждое совпадение

+ pending_reviews.match_card_message_id/_thread_id — куда дописать решение
оператора по ответу на проверке (карточка совпадения этого сообщения).

Revision ID: yy23matches
Revises: xx22delay
Create Date: 2026-09-29

ВНИМАНИЕ при слиянии: ww21stage (ww21_lead_stage.py, в работе в основной
ветке) тоже ссылается на xx22delay. Какая из двух ревизий приедет второй —
та должна указать down_revision на первую (например, yy23matches →
"ww21stage"), иначе у alembic будет две головы и `alembic upgrade head` в
сервисе migrate упадёт.
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
    op.add_column(
        "pending_reviews", sa.Column("match_card_message_id", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "pending_reviews", sa.Column("match_card_thread_id", sa.BigInteger(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("pending_reviews", "match_card_thread_id")
    op.drop_column("pending_reviews", "match_card_message_id")
    op.drop_column("notify_settings", "log_all_matches")
