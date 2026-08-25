"""knowledge: embedding nullable + полнотекстовый индекс (FTS-RAG)

Revision ID: mm11kbfts
Revises: ll10abtest
Create Date: 2026-08-25
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "mm11kbfts"
down_revision: str | None = "ll10abtest"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Провайдер не даёт эмбеддингов — ищем по полнотексту, embedding опционален.
    op.execute("ALTER TABLE knowledge_chunks ALTER COLUMN embedding DROP NOT NULL")
    # GIN-индекс по русскому полнотексту содержимого чанка.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_fts "
        "ON knowledge_chunks USING gin (to_tsvector('russian', content))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_knowledge_chunks_fts")
    # Обратно NOT NULL не возвращаем: старые чанки могут быть без эмбеддинга.
