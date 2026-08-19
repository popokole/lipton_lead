"""База знаний и векторный поиск (ТЗ §16, §17).

Размерность вектора фиксируется на уровне базы знаний, а не глобально: сменить
модель эмбеддингов = переиндексировать документы, поэтому смешивать модели
внутри одной базы нельзя.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models._types import pg_enum
from app.models.enums import KnowledgeDocumentStatus

# Размерность text-embedding-3-small. Колонка pgvector требует константы, а не
# значения из настроек, поэтому смена модели на другую размерность — миграция.
EMBEDDING_DIM = 1536


class KnowledgeBase(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "knowledge_bases"

    name: Mapped[str] = mapped_column(sa.String(120), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(sa.Text)
    embedding_model: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    dim: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=EMBEDDING_DIM)
    similarity_threshold: Mapped[float] = mapped_column(
        sa.Numeric(3, 2), nullable=False, default=0.75
    )
    max_chunks: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=5)

    __table_args__ = (
        sa.CheckConstraint(
            "similarity_threshold >= 0 AND similarity_threshold <= 1",
            name="similarity_threshold_range",
        ),
        sa.CheckConstraint("max_chunks > 0", name="max_chunks_positive"),
    )


class KnowledgeDocument(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "knowledge_documents"

    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
    )
    filename: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    mime: Mapped[str | None] = mapped_column(sa.String(120))
    size_bytes: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    # Хеш содержимого: повторная загрузка того же файла не плодит дубли чанков.
    sha256: Mapped[str] = mapped_column(sa.String(64), nullable=False)

    status: Mapped[KnowledgeDocumentStatus] = mapped_column(
        pg_enum(KnowledgeDocumentStatus, "knowledge_document_status"),
        nullable=False,
        default=KnowledgeDocumentStatus.PENDING,
    )
    chunk_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(sa.Text)

    __table_args__ = (
        sa.UniqueConstraint(
            "knowledge_base_id", "sha256", name="uq_knowledge_documents_knowledge_base_id_sha256"
        ),
        sa.Index("ix_knowledge_documents_status", "status"),
    )


class KnowledgeChunk(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "knowledge_chunks"

    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        sa.ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    ord: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    content: Mapped[str] = mapped_column(sa.Text, nullable=False)
    tokens: Mapped[int | None] = mapped_column(sa.Integer)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)

    __table_args__ = (
        sa.UniqueConstraint("document_id", "ord", name="uq_knowledge_chunks_document_id_ord"),
        sa.Index("ix_knowledge_chunks_knowledge_base_id", "knowledge_base_id"),
        sa.Index(
            "ix_knowledge_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )
