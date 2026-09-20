"""Append-only per-print usage journal — the persisted source of attribution truth.

One row per event; the spool identity is FROZEN at event time (both backends,
same row) so completion never re-asks the DB "whose spool was this" for a
journaled print. Rows outlive the print for forensics and are pruned by the
``usage_events_retention_hours`` sweep plus the archive hard-delete path —
the FK CASCADE below fires on PostgreSQL only (SQLite never sets
``PRAGMA foreign_keys``).
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base

EVENT_START = "start"
EVENT_TRAY_CHANGE = "tray_change"
EVENT_RUNOUT = "runout"
EVENT_SPOOL_LOADED = "spool_loaded"
EVENT_PAUSE = "pause"
EVENT_RESUME = "resume"

KIND_PAUSE = "pause"
KIND_AUTOSWITCH = "autoswitch"
KIND_EXTERNAL = "external"
KIND_AMBIGUOUS = "ambiguous"
# A deliberate mid-pause spool change the human declared via the assignment
# prompt — no firmware event witnesses it. Shares the ambiguous contract:
# a segment boundary only through spool_loaded, never a zero correction.
KIND_MANUAL = "manual"


class PrintUsageEvent(Base):
    __tablename__ = "print_usage_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int] = mapped_column(ForeignKey("printers.id", ondelete="CASCADE"), index=True)
    archive_id: Mapped[int] = mapped_column(ForeignKey("print_archives.id", ondelete="CASCADE"), index=True)
    layer_num: Mapped[int] = mapped_column(Integer)
    event: Mapped[str] = mapped_column(String(24))
    kind: Mapped[str | None] = mapped_column(String(24), nullable=True)
    global_tray_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    spool_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    spoolman_spool_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Did the AMS's own presence sensor report filament in this event's slot at
    # the moment it was written (``tray_exist_bits`` → ``exists``). NULL is
    # "no reading", never "empty" — see m161. Readers must keep their previous
    # behaviour on NULL.
    slot_occupied: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
