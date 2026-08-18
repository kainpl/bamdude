"""Schemas for the library trash bin + bulk purge (#1008)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class PurgePreviewResponse(BaseModel):
    count: int
    total_bytes: int
    sample_filenames: list[str]
    older_than_days: int
    include_never_printed: bool


class PurgeRequest(BaseModel):
    older_than_days: int = Field(ge=1, le=3650)
    include_never_printed: bool = True


class PurgeResponse(BaseModel):
    moved_to_trash: int


class TrashFile(BaseModel):
    id: int
    filename: str
    file_size: int
    thumbnail_path: str | None = None
    folder_id: int | None = None
    folder_name: str | None = None
    created_by_id: int | None = None
    created_by_username: str | None = None
    deleted_at: datetime
    auto_purge_at: datetime


class TrashListResponse(BaseModel):
    items: list[TrashFile]
    total: int
    retention_days: int


class TrashSettings(BaseModel):
    retention_days: int = Field(ge=1, le=365)
    auto_purge_enabled: bool = False
    auto_purge_days: int = Field(default=90, ge=7, le=3650)
    auto_purge_include_never_printed: bool = True


class LibraryAutoPurgeLastRun(BaseModel):
    started_at: str
    finished_at: str | None = None
    # ``moved`` = -1 means "ran but the count was lost on process restart".
    moved: int


class LibraryAutoPurgeStatus(BaseModel):
    enabled: bool
    days: int
    include_never_printed: bool
    last_run: LibraryAutoPurgeLastRun | None = None
    next_run_at: str | None = None


class EmptyTrashResponse(BaseModel):
    deleted: int
    skipped_pinned: int = 0


class TrashRestoreCheckRequest(BaseModel):
    """Which trashed files the caller is about to restore."""

    ids: list[int] = Field(default_factory=list)


class TrashRestoreConflict(BaseModel):
    """A trashed file whose content an active file already holds.

    Restoring it would recreate the byte-identical pair every ingest path now
    refuses to make, so the caller asks before doing it — and names the file
    that is already there, because "this is a duplicate" is not actionable
    without knowing of what.
    """

    id: int
    filename: str
    existing_id: int
    existing_filename: str
