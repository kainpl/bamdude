from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base


class PrintQueueItem(Base):
    """Print queue item — always assigned to a specific printer's queue."""

    __tablename__ = "print_queue"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Queue assignment (required — every item belongs to a printer's queue)
    queue_id: Mapped[int] = mapped_column(ForeignKey("printer_queues.id"), nullable=False)

    # Waiting reason — why this pending item hasn't started yet
    # e.g. "Plate not cleared", "Printer offline", "Drying in progress", "Previous print failed"
    waiting_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Source file (either archive_id OR library_file_id; archive created at print start from library file)
    archive_id: Mapped[int | None] = mapped_column(ForeignKey("print_archives.id", ondelete="CASCADE"), nullable=True)
    library_file_id: Mapped[int | None] = mapped_column(
        ForeignKey("library_files.id", ondelete="CASCADE"), nullable=True
    )
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)

    # Scheduling
    position: Mapped[int] = mapped_column(Integer, default=0)
    scheduled_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # None = ASAP
    manual_start: Mapped[bool] = mapped_column(Boolean, default=False)

    # Power management
    auto_off_after: Mapped[bool] = mapped_column(Boolean, default=False)

    # AMS mapping: JSON array of global tray IDs per filament slot
    # Format: "[5, -1, 2, -1]" — position=slot_id-1, value=global tray ID, -1=unused
    ams_mapping: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Plate ID for multi-plate 3MF files (1-indexed, None = plate 1)
    plate_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Print options
    bed_levelling: Mapped[bool] = mapped_column(Boolean, default=True)
    flow_cali: Mapped[bool] = mapped_column(Boolean, default=True)
    vibration_cali: Mapped[bool] = mapped_column(Boolean, default=False)
    layer_inspect: Mapped[bool] = mapped_column(Boolean, default=False)
    timelapse: Mapped[bool] = mapped_column(Boolean, default=False)
    use_ams: Mapped[bool] = mapped_column(Boolean, default=True)

    # Status: pending, printing, completed, failed, skipped, cancelled
    status: Mapped[str] = mapped_column(String(20), default="pending")

    # Batch grouping — UUID string shared by all items created together via quantity>1.
    # Nullable: single-copy adds (quantity=1) leave this unset.
    batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    # Tracking
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # User tracking
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    # Relationships
    queue: Mapped["PrinterQueue"] = relationship(back_populates="items")
    archive: Mapped["PrintArchive | None"] = relationship()
    library_file: Mapped["LibraryFile | None"] = relationship()
    project: Mapped["Project | None"] = relationship(back_populates="queue_items")
    created_by: Mapped["User | None"] = relationship()

    # Convenience property to get printer_id via queue
    @property
    def printer_id(self) -> int | None:
        """Get printer_id from the queue relationship."""
        return self.queue.printer_id if self.queue else None


from backend.app.models.archive import PrintArchive  # noqa: E402
from backend.app.models.library import LibraryFile  # noqa: E402
from backend.app.models.printer_queue import PrinterQueue  # noqa: E402
from backend.app.models.project import Project  # noqa: E402
from backend.app.models.user import User  # noqa: E402
