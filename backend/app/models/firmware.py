"""Firmware bulk-update + cache models.

- ``FirmwareCacheEntry`` indexes each downloaded firmware file kept under
  ``<data_dir>/firmware/`` so it is reusable by ``(model, version)`` even after
  the vendor removes the version from its site (no download URL needed to find it).
- ``FirmwareBatchRun`` / ``FirmwareBatchItem`` record one bulk firmware operation
  and its per-printer outcomes for audit + history.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class FirmwareCacheEntry(Base):
    """Index row for one downloaded firmware file kept under <data_dir>/firmware/.

    The file is reusable by ``(model, version)`` even after the vendor removes the
    version from its site, because we no longer need the download URL to find it.
    """

    __tablename__ = "firmware_cache_entries"
    __table_args__ = (UniqueConstraint("model", "version", name="uq_firmware_cache_model_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    model: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    release_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    downloaded_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class FirmwareBatchRun(Base):
    """One bulk firmware operation."""

    __tablename__ = "firmware_batch_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # "bulk" = the mass-update page; "single" = the per-printer firmware modal.
    source: Mapped[str] = mapped_column(String(16), default="bulk")
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|completed|failed
    total: Mapped[int] = mapped_column(Integer, default=0)
    succeeded: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)


class FirmwareBatchItem(Base):
    """Per-printer outcome within a run."""

    __tablename__ = "firmware_batch_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("firmware_batch_runs.id"), index=True)
    printer_id: Mapped[int] = mapped_column(ForeignKey("printers.id"))
    model: Mapped[str] = mapped_column(String(64))
    from_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    to_version: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(16), default="download_only")  # download_only|remote_apply
    status: Mapped[str] = mapped_column(String(16), default="pending")
    # pending|skipped|uploading|uploaded|applying|applied|failed
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
