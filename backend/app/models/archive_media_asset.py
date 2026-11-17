"""A video that belongs to one print archive (m237, G10 C).

``print_archives.timelapse_path`` says which video an archive shows. It cannot
say where that video came from, which revision of the archive's video it is, or
that a recording on a printer has already been taken by one archive and must
not be attached to another. This table does: one row per published video, its
identity and revision, and — for a recording read off a printer — the source it
was read from.

It is not a table of physical runs and it merges no histories: an archive is
still one print (``inv-archive-is-print-history``). ``timelapse_path`` stays, as
the projection of the ``primary`` row, written in the same transaction.

Only ``services/archive_media.py`` writes this table.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base

ROLE_PRIMARY = "primary"  # the video ``timelapse_path`` points at; at most one per archive
ROLE_VARIANT = "variant"  # another video of the same archive, kept beside it ("save as new")
ROLE_RETIRED = "retired"  # replaced or removed; its file is waiting to be unlinked
# A printer recording this archive downloaded, whose video has since been replaced: no local file
# is implied, the row only keeps the claim (and the hash of the bytes that were downloaded).
ROLE_RECEIPT = "receipt"

ORIGIN_PRINTER = "printer"  # read off the printer's storage
ORIGIN_UPLOAD = "upload"  # sent by an operator
ORIGIN_CONVERSION = "conversion"  # AVI → MP4
ORIGIN_EDITOR = "editor"
ORIGIN_LAYER = "layer"  # stitched from the camera's per-layer frames
ORIGIN_LEGACY = "legacy"  # attached before this table existed: nothing is known about its source


class ArchiveMediaAsset(Base):
    __tablename__ = "archive_media_assets"
    __table_args__ = (
        UniqueConstraint("asset_uuid", name="uq_archive_media_assets_asset_uuid"),
        # One primary video per archive. A partial index: variants and retired rows are not limited.
        Index(
            "uq_archive_media_assets_primary",
            "archive_id",
            unique=True,
            sqlite_where=text("role = 'primary'"),
            postgresql_where=text("role = 'primary'"),
        ),
        # A recording on a printer belongs to one archive: the printer's immutable identity, the
        # medium, the normalised path and the SHA-256 of the bytes actually downloaded. NULLs are
        # distinct on both engines, so rows without a remote source never collide.
        Index(
            "uq_archive_media_assets_remote_claim",
            "remote_printer_identity",
            "remote_storage",
            "remote_path",
            "content_sha256",
            unique=True,
        ),
        Index("ix_archive_media_assets_archive", "archive_id", "role"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # The identity jobs, pins and the revision check name. Never the numeric id:
    # SQLite hands a deleted row's id to the next one.
    asset_uuid: Mapped[str] = mapped_column(String(36), nullable=False)
    # SQLite runs no FK actions: ``archive_media.forget_archives`` removes the rows
    # wherever an archive is hard-deleted; the CASCADE is PostgreSQL's backstop.
    archive_id: Mapped[int] = mapped_column(ForeignKey("print_archives.id", ondelete="CASCADE"), nullable=False)
    # The archive's ``recovery_identity`` when the row was written, compared on
    # every read: an id reused by a later archive does not inherit the video.
    archive_identity: Mapped[str] = mapped_column(String(36), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    # Per archive, growing by one with every publication. "No video" is revision 0.
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    # Relative to DATA_DIR, exactly as ``timelapse_path`` stores it.
    relative_path: Mapped[str] = mapped_column(String(500), nullable=False)
    # NULL only for a legacy row: nothing was measured when it was attached.
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    # The source on the printer — set only for ``origin = 'printer'``.
    # ``Printer.connector_identity``, never the numeric printer id.
    remote_printer_identity: Mapped[str | None] = mapped_column(String(36), nullable=True)
    remote_storage: Mapped[str | None] = mapped_column(String(16), nullable=True)
    remote_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    remote_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # The listing's own text for the time: an FTP listing gives a wall clock with no zone.
    remote_mtime_raw: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Stamped by the writer in Python UTC — a server default is local time on a
    # PostgreSQL whose timezone is not UTC.
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
