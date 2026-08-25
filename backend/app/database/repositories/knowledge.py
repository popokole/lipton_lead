"""База знаний на полнотекстовом поиске (RAG без эмбеддингов).

Провайдер (codex.sale) не отдаёт эмбеддинги, поэтому поиск идёт по русскому
FTS Postgres. Документ режется на чанки, чанки индексируются to_tsvector, а на
запрос возвращаются самые релевантные куски — их конвейер отдаёт генератору как
контекст (knowledge=[...]).
"""

from __future__ import annotations

import hashlib
import re
import uuid

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KnowledgeBase, KnowledgeChunk, KnowledgeDocument
from app.models.enums import KnowledgeDocumentStatus

# Целевой размер чанка в символах: достаточно, чтобы кусок был осмысленным, но
# не настолько большой, чтобы забить контекст одним документом.
CHUNK_TARGET = 700
CHUNK_MAX = 1000


def chunk_text(raw: str) -> list[str]:
    """Режет текст на смысловые куски по абзацам, склеивая мелкие."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", raw) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        if len(para) > CHUNK_MAX:
            # Слишком длинный абзац — режем по предложениям.
            for sentence in re.split(r"(?<=[.!?])\s+", para):
                if len(buf) + len(sentence) > CHUNK_TARGET and buf:
                    chunks.append(buf.strip())
                    buf = ""
                buf += sentence + " "
            continue
        if len(buf) + len(para) > CHUNK_TARGET and buf:
            chunks.append(buf.strip())
            buf = ""
        buf += para + "\n"
    if buf.strip():
        chunks.append(buf.strip())
    return [c for c in chunks if c]


class KnowledgeRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # --- базы ---------------------------------------------------------------
    async def list_bases(self) -> list[KnowledgeBase]:
        rows = await self._db.scalars(select(KnowledgeBase).order_by(KnowledgeBase.created_at))
        return list(rows.all())

    async def create_base(self, name: str, description: str | None) -> KnowledgeBase:
        kb = KnowledgeBase(
            name=name,
            description=description,
            # Модель эмбеддингов не используется (FTS), но колонка NOT NULL.
            embedding_model="fts",
        )
        self._db.add(kb)
        await self._db.flush()
        return kb

    async def delete_base(self, kb_id: uuid.UUID) -> None:
        await self._db.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))

    async def stats(self, kb_id: uuid.UUID) -> tuple[int, int]:
        docs = await self._db.scalar(
            select(func.count())
            .select_from(KnowledgeDocument)
            .where(KnowledgeDocument.knowledge_base_id == kb_id)
        )
        chunks = await self._db.scalar(
            select(func.count())
            .select_from(KnowledgeChunk)
            .where(KnowledgeChunk.knowledge_base_id == kb_id)
        )
        return int(docs or 0), int(chunks or 0)

    # --- документы ----------------------------------------------------------
    async def add_text(self, kb_id: uuid.UUID, title: str, body: str) -> KnowledgeDocument:
        """Добавляет текстовый документ: режет на чанки и индексирует."""
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        doc = KnowledgeDocument(
            knowledge_base_id=kb_id,
            filename=title[:255] or "doc",
            mime="text/plain",
            size_bytes=len(body.encode("utf-8")),
            sha256=digest,
            status=KnowledgeDocumentStatus.PARSING,
        )
        self._db.add(doc)
        await self._db.flush()

        pieces = chunk_text(body)
        for ordinal, piece in enumerate(pieces):
            self._db.add(
                KnowledgeChunk(
                    knowledge_base_id=kb_id,
                    document_id=doc.id,
                    ord=ordinal,
                    content=piece,
                    embedding=None,
                )
            )
        doc.chunk_count = len(pieces)
        doc.status = KnowledgeDocumentStatus.READY
        await self._db.flush()
        return doc

    async def list_documents(self, kb_id: uuid.UUID) -> list[KnowledgeDocument]:
        rows = await self._db.scalars(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.knowledge_base_id == kb_id)
            .order_by(KnowledgeDocument.created_at.desc())
        )
        return list(rows.all())

    async def delete_document(self, document_id: uuid.UUID) -> None:
        await self._db.execute(
            delete(KnowledgeDocument).where(KnowledgeDocument.id == document_id)
        )

    # --- поиск --------------------------------------------------------------
    async def retrieve(self, kb_id: uuid.UUID, query: str, limit: int = 5) -> list[str]:
        """Топ релевантных чанков по русскому FTS.

        Термины объединяются по OR (совпадение по любому слову), а не AND: иначе
        «сколько стоит терапия» не найдёт «стоимость терапии». Ранжируем по
        ts_rank — куски с большим числом совпадений идут первыми.
        """
        words = [w for w in re.findall(r"\w+", (query or "").lower()) if len(w) >= 3]
        if not words:
            return []
        tsq = " | ".join(words)
        stmt = (
            select(KnowledgeChunk.content)
            .where(
                KnowledgeChunk.knowledge_base_id == kb_id,
                text("to_tsvector('russian', content) @@ to_tsquery('russian', :q)"),
            )
            .order_by(
                text(
                    "ts_rank(to_tsvector('russian', content), to_tsquery('russian', :q)) DESC"
                )
            )
            .limit(limit)
        )
        rows = await self._db.execute(stmt, {"q": tsq})
        return [r[0] for r in rows.all()]
