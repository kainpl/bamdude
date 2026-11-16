"""A photo an operator attached to a library file (m233).

One row per photo; the bytes live in ``<library>/photos/<library_file_id>/``
under ``stored_name`` (and a small copy beside it). Only
``services/library_file_photos.py`` writes this table and that folder.

The photos belong to the library ROW — not to its bytes: another row with the
same content hash has none of them, and an archive made from the file has none
either.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class LibraryFilePhoto(Base):
    __tablename__ = "library_file_photos"
    # A photo's id is in its URL, which a browser caches: it must never pass to
    # another photo. SQLite without AUTOINCREMENT hands out ``max(id)+1``.
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # SQLite runs no FK actions: the writer's ``forget_files`` removes the rows
    # wherever a library file is purged; the CASCADE is PostgreSQL's backstop.
    library_file_id: Mapped[int] = mapped_column(
        ForeignKey("library_files.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # ``<32 hex>.<png|jpg|webp>`` — chosen by the writer, never by the client.
    stored_name: Mapped[str] = mapped_column(String(64), nullable=False)
    # The client's name for the file, for display only: a base name, never a path.
    original_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # What is on disk — the photo saved again, not the upload.
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    caption: Mapped[str | None] = mapped_column(String(500), nullable=True)
    uploaded_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # A snapshot: the name stays when the user is deleted.
    uploaded_by_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    # Stamped by the writer in Python UTC — a server default is local time on a
    # PostgreSQL whose timezone is not UTC.
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
