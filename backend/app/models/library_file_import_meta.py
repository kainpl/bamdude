"""Where a library file came from, when a model source imported it (m235).

One row per imported library file: the source, the model's and the file's id
there, and the model's title, creator and licence as they were at the import —
the licence of a model can change later, the row says what it was. Provider
neutral; MakerWorld keeps its own richer table (m056) and writes nothing here.

Only ``services/library_import_meta.py`` writes this table.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class LibraryFileImportMeta(Base):
    __tablename__ = "library_file_import_meta"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # SQLite runs no FK actions: ``forget_files`` removes the row wherever a
    # library file is purged; the CASCADE is PostgreSQL's backstop.
    library_file_id: Mapped[int] = mapped_column(
        ForeignKey("library_files.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # TEXT ids: a self-hosted source's identifiers are not numbers.
    source_model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_file_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    creator: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # An SPDX id (``CC-BY-4.0``) or the source's own name for the licence.
    license: Mapped[str | None] = mapped_column(String(128), nullable=True)
    canonical_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Stamped by the writer in Python UTC.
    imported_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
