"""Durable copy of the filament-attribution context for an in-flight print."""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class ActivePrintSession(Base):
    """Print-start context the completion path needs, persisted per printer.

    ``usage_tracker._active_sessions`` holds the same data in memory, and the
    tray-change log lives on ``PrinterState``. Both die with the process, so a
    restart mid-print silently destroys filament attribution: without the
    assignment snapshot a spool unlinked at runout can't be resolved, and
    without the tray-change log an AMS-backup switch charges the whole print to
    whichever tray happened to finish it — the spool that actually ran dry is
    charged nothing.

    One row per printer — a printer runs one print at a time. Written at print
    start, appended to on every tray change, deleted at completion. A leaked row
    (a completion we never saw) is harmless: print start overwrites it, and the
    restore path refuses a row whose ``print_name`` isn't what the printer says
    it is running.

    ⚠️ **No ``plate_id`` here, unlike upstream.** Our plate authority is
    ``PrintArchive.plate_index``, which is already in the database — the
    completion path reads it there and so survives a restart by construction.
    Copying it into a second row would be a second source of truth for the same
    question.

    The Spoolman writer has had an equivalent durable row since #1820
    (``active_print_spoolman``); this is the internal-inventory counterpart.

    No migration: ``Base.metadata.create_all()`` at startup creates a *missing
    table* on existing databases (only new *columns* need one) — same as
    ``active_print_spoolman``, whose first migration (m101) added a column to a
    table create_all had already made.
    """

    __tablename__ = "active_print_sessions"

    printer_id: Mapped[int] = mapped_column(ForeignKey("printers.id", ondelete="CASCADE"), primary_key=True)

    print_name: Mapped[str] = mapped_column(default="")
    started_at: Mapped[datetime] = mapped_column(DateTime)

    # tray_now at print start — reliable, unlike at completion where the printer
    # has usually retracted and reports 255.
    tray_now_at_start: Mapped[int] = mapped_column(default=-1)

    # Slicer slot -> global tray, as dispatched: [2]. The live MQTT ``mapping``
    # field is not a substitute — AMS backup rewrites it to the substitute tray.
    ams_mapping: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # {"<ams_id>-<tray_id>": spool_id} — the assignment map as it stood before
    # the print could disturb it.
    spool_assignments: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # {"<ams_id>-<tray_id>": remain%} for the remain-delta fallback path.
    tray_remain_start: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # [[global_tray_id, layer_num], ...] mirroring PrinterState.tray_change_log.
    tray_change_log: Mapped[list | None] = mapped_column(JSON, nullable=True)
