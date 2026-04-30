"""Schemas for archive auto-purge + trash (#1008 follow-up)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ArchivePurgePreviewResponse(BaseModel):
    count: int
    total_bytes: int
    sample_filenames: list[str]
    older_than_days: int


class ArchivePurgeRequest(BaseModel):
    older_than_days: int = Field(ge=1, le=3650)


class ArchivePurgeResponse(BaseModel):
    moved_to_trash: int


class ArchivePurgeSettings(BaseModel):
    enabled: bool = False
    days: int = Field(default=365, ge=7, le=3650)


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
