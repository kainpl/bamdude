"""Printer queue model - one queue per printer with status tracking."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base


class PrinterQueue(Base):
    """Per-printer queue with status, counters, and activity tracking."""

    __tablename__ = "printer_queues"

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int] = mapped_column(Integer, ForeignKey("printers.id", ondelete="CASCADE"), unique=True)

    # Queue status: idle, printing, paused, error
    status: Mapped[str] = mapped_column(String(20), default="idle")

    # Activity tracking
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    current_item_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Cached counters. Only live-state counters (pending, skipped) live here
    # post-m019 — completed / failed / cancelled / total roll off the archive
    # table via archive.queue_id at read time, since queue items in those
    # terminal states are cleaned up or never existed (external prints).
    pending_count: Mapped[int] = mapped_column(default=0)
    skipped_count: Mapped[int] = mapped_column(default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
    printer = relationship("Printer", back_populates="queue")
    items = relationship("PrintQueueItem", back_populates="queue", lazy="dynamic")

    def __repr__(self) -> str:
        return f"<PrinterQueue printer_id={self.printer_id} status={self.status}>"
