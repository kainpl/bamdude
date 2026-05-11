"""Detailed MakerWorld metadata attached 1:1 to a library file.

Captured at import time (and back-filled by m056 for legacy imports). The
parent ``library_files`` row carries operational info (path, size, hash,
folder); this child table carries the *source-of-truth* MakerWorld view —
title, description, author, variant, license, compatibility, cover-image
local paths, the raw payload, etc.

One-to-one with ``library_files`` (UNIQUE FK + ondelete CASCADE) so a
``library_file`` delete (hard or trash purge) automatically drops the
meta row and its on-disk covers — wired up via the deletion path in
``api/routes/library.py``.
"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base


class LibraryFileMakerworldMeta(Base):
    """MakerWorld source metadata for a library file."""

    __tablename__ = "library_file_makerworld_meta"

    id: Mapped[int] = mapped_column(primary_key=True)
    library_file_id: Mapped[int] = mapped_column(
        ForeignKey("library_files.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )

    # Model-level
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    author_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    author_profile_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    license: Mapped[str | None] = mapped_column(String(64), nullable=True)
    original_design_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Variant (plate / profile) level
    variant_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    variant_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    variant_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    profile_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    # Local cover images. Relative paths under ``base_dir`` — same shape as
    # ``library_files.thumbnail_path`` so existing path-resolving helpers
    # work without special-casing.
    cover_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    variant_cover_path: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Compatibility / printability
    sliced_for: Mapped[str | None] = mapped_column(String(64), nullable=True)
    compatible_models: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    needs_ams: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    material_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    materials: Mapped[list[dict] | None] = mapped_column(JSON, nullable=True)

    # Re-fetch handles. ``model_id_alphanumeric`` is required by
    # api.bambulab.com to mint signed download URLs (cf. YASTL#51 path).
    model_id_alphanumeric: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Forensic blob — full design+instance payload at import time. Lets us
    # surface new fields later without re-hitting MakerWorld and survives
    # upstream taking the model down. Pruned alongside the parent row.
    raw_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    imported_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    library_file: Mapped["LibraryFile"] = relationship(  # type: ignore[name-defined]  # noqa: F821
        "LibraryFile",
        back_populates="makerworld_meta",
        lazy="joined",
    )
