"""Per-part state of one printed plate (m158).

Seeded wherever an archive gains its 3MF (dispatch, attach, adoption,
backfill); the live counterpart of the flat ``PrintArchive.defective_count``.
``identify_ids`` is the M623 id space the firmware skips by — the skip
callback intersects against it without reopening any file.
"""

from sqlalchemy import JSON, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class PrintArchivePart(Base):
    __tablename__ = "print_archive_parts"

    id: Mapped[int] = mapped_column(primary_key=True)
    archive_id: Mapped[int] = mapped_column(ForeignKey("print_archives.id", ondelete="CASCADE"), index=True)
    # Canonical name, original spelling (display); name_key is the
    # lowercased aggregation key — see services/part_names.py.
    name: Mapped[str] = mapped_column(String(512))
    name_key: Mapped[str] = mapped_column(String(512), index=True)
    identify_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    defective: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
