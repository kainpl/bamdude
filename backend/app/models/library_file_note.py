"""User-authored notes attached to library files (gh#3)."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class LibraryFileNote(Base):
    """A free-form note attached to a library file.

    Multiple notes per file. CASCADE on `library_file_id` — the note dies
    with its file. SET NULL on `user_id` — the note survives if the
    author's account is deleted (anonymised).
    """

    __tablename__ = "library_file_notes"
    __table_args__ = (Index("ix_library_file_notes_file", "library_file_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    library_file_id: Mapped[int] = mapped_column(ForeignKey("library_files.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    body: Mapped[str] = mapped_column(String(1000), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
