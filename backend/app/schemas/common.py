"""Общие схемы ответов."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page[T](BaseModel):
    items: list[T]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class Ok(BaseModel):
    ok: bool = True
    detail: str | None = None
