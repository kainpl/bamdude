"""Schemas for archive trash (#1008 follow-up)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ArchiveTrashItem(BaseModel):
    id: int
    filename: str
    print_name: str | None = None
    file_size: int | None = None
    thumbnail_path: str | None = None
    printer_id: int | None = None
    project_id: int | None = None
    status: str | None = None
    created_by_id: int | None = None
    created_by_username: str | None = None
    deleted_at: datetime
    auto_purge_at: datetime


class ArchiveTrashListResponse(BaseModel):
    items: list[ArchiveTrashItem]
    total: int
    retention_days: int


class ArchiveTrashSettings(BaseModel):
    retention_days: int = Field(ge=1, le=365)


class ArchiveEmptyTrashResponse(BaseModel):
    deleted: int
