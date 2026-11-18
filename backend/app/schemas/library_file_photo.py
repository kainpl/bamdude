"""Schemas for a library file's photos (m233)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from backend.app.schemas.not_null import IgnoresNullForRequiredColumns


class LibraryFilePhotoResponse(BaseModel):
    """One photo. The name its bytes are stored under is not part of the answer:
    a client addresses a photo by its id, through the file it belongs to."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    library_file_id: int
    original_name: str | None = None
    content_type: str
    file_size: int
    width: int
    height: int
    caption: str | None = None
    uploaded_by_name: str | None = None
    created_at: datetime


class LibraryFilePhotoUpdate(IgnoresNullForRequiredColumns):
    """``null`` or an empty string clears the caption. The length is the writer's
    to refuse, with a sentence the error boundary can translate."""

    __orm_table__ = "library_file_photos"

    caption: str | None = None
