"""Schemas for library file notes (gh#3)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

NOTE_MAX_LENGTH = 1000


class LibraryFileNoteCreate(BaseModel):
    body: str = Field(..., min_length=1, max_length=NOTE_MAX_LENGTH)


class LibraryFileNoteUpdate(BaseModel):
    body: str = Field(..., min_length=1, max_length=NOTE_MAX_LENGTH)


class LibraryFileNoteResponse(BaseModel):
    """Note plus joined username for display + author-edit flag.

    `can_edit` is a denormalised convenience for the frontend: True when the
    note belongs to the requesting user (or when authentication is disabled
    and there is no current user). The backend ALSO enforces ownership at
    the route level — `can_edit` is purely a UI hint, not a security gate.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    library_file_id: int
    user_id: int | None = None
    user_username: str | None = None
    body: str
    created_at: datetime
    updated_at: datetime
    can_edit: bool = False
