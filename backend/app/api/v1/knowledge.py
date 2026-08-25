"""База знаний (RAG на полнотекстовом поиске).

Документы добавляются текстом (вставка), режутся на чанки и индексируются FTS.
Сценарий ссылается на базу через scenario.knowledge_base_id — тогда конвейер
подмешивает найденные куски в ответ.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, DbDep, OperatorUser
from app.database.repositories.knowledge import KnowledgeRepository
from app.schemas.common import Ok

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


class KbOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    documents: int
    chunks: int
    created_at: datetime


class KbCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)


class DocOut(BaseModel):
    id: uuid.UUID
    filename: str
    chunk_count: int
    status: str
    created_at: datetime


class DocCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=100_000)


@router.get("", response_model=list[KbOut], summary="Список баз знаний")
async def list_bases(_user: CurrentUser, db: DbDep) -> list[KbOut]:
    repo = KnowledgeRepository(db)
    out: list[KbOut] = []
    for kb in await repo.list_bases():
        docs, chunks = await repo.stats(kb.id)
        out.append(
            KbOut(
                id=kb.id,
                name=kb.name,
                description=kb.description,
                documents=docs,
                chunks=chunks,
                created_at=kb.created_at,
            )
        )
    return out


@router.post("", response_model=KbOut, status_code=201, summary="Создать базу знаний")
async def create_base(payload: KbCreate, _user: OperatorUser, db: DbDep) -> KbOut:
    kb = await KnowledgeRepository(db).create_base(payload.name.strip(), payload.description)
    return KbOut(
        id=kb.id,
        name=kb.name,
        description=kb.description,
        documents=0,
        chunks=0,
        created_at=kb.created_at,
    )


@router.delete("/{kb_id}", response_model=Ok, summary="Удалить базу знаний")
async def delete_base(kb_id: uuid.UUID, _user: OperatorUser, db: DbDep) -> Ok:
    await KnowledgeRepository(db).delete_base(kb_id)
    return Ok()


@router.get("/{kb_id}/documents", response_model=list[DocOut], summary="Документы базы")
async def list_documents(kb_id: uuid.UUID, _user: CurrentUser, db: DbDep) -> list[DocOut]:
    docs = await KnowledgeRepository(db).list_documents(kb_id)
    return [
        DocOut(
            id=d.id,
            filename=d.filename,
            chunk_count=d.chunk_count,
            status=d.status.value,
            created_at=d.created_at,
        )
        for d in docs
    ]


@router.post("/{kb_id}/documents", response_model=DocOut, status_code=201, summary="Добавить текст")
async def add_document(
    kb_id: uuid.UUID, payload: DocCreate, _user: OperatorUser, db: DbDep
) -> DocOut:
    doc = await KnowledgeRepository(db).add_text(kb_id, payload.title.strip(), payload.body)
    return DocOut(
        id=doc.id,
        filename=doc.filename,
        chunk_count=doc.chunk_count,
        status=doc.status.value,
        created_at=doc.created_at,
    )


@router.delete("/document/{document_id}", response_model=Ok, summary="Удалить документ")
async def delete_document(document_id: uuid.UUID, _user: OperatorUser, db: DbDep) -> Ok:
    await KnowledgeRepository(db).delete_document(document_id)
    return Ok()


@router.get("/{kb_id}/search", response_model=list[str], summary="Тест поиска по базе")
async def search(
    kb_id: uuid.UUID, _user: CurrentUser, db: DbDep, q: str = Query(min_length=1)
) -> list[str]:
    return await KnowledgeRepository(db).retrieve(kb_id, q, limit=5)
