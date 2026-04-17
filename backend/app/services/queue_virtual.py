"""Synthesise a virtual ``PrintQueueItemResponse`` for active prints that
have no corresponding ``PrintQueueItem`` row.

When a print is started outside the BamDude queue (direct "Print Now",
slicer upload to the printer, cloud start, printer-screen start) there
is no real queue item to display.  This helper builds a read-only
pseudo-item from the printer's live MQTT state + the resolved archive,
shaped identically to ``PrintQueueItemResponse`` so the frontend can
render it without special-case code.

Distinguishing features on the returned dict:

* ``is_virtual = True`` — UI gates edit/cancel/reorder on this.
* ``source`` — ``'bamdude_direct'`` if the print was dispatched via
  BamDude (Print Now, Reprint, Library direct) or ``'external'`` for
  everything else.
* ``id = -printer_id`` — negative id never collides with real rows.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.printer_manager import printer_manager

logger = logging.getLogger(__name__)

# MQTT states that count as "active print" for virtual-item purposes.
_ACTIVE_STATES = {"RUNNING", "PREPARE", "PAUSE", "PAUSED"}


async def build_virtual_current_print(
    db: AsyncSession,
    printer_id: int,
) -> dict[str, Any] | None:
    """Return a ``PrintQueueItemResponse``-shaped dict for the printer's
    active print, or ``None`` when no virtual item is needed.

    Returns ``None`` when:
      * Printer isn't connected or state isn't one of the active states.
      * A real ``PrintQueueItem`` with ``status='printing'`` already
        exists for this printer's queue — the real one wins.
      * No archive is tracked in ``_active_prints`` for this printer.
    """
    state = printer_manager.get_status(printer_id)
    if state is None or not state.connected:
        return None
    if state.state not in _ACTIVE_STATES:
        return None

    # Real queue_item wins — caller's list endpoint already includes it.
    queue_row = (
        await db.execute(select(PrinterQueue).where(PrinterQueue.printer_id == printer_id))
    ).scalar_one_or_none()
    if queue_row is None:
        return None

    real_printing = (
        await db.execute(
            select(PrintQueueItem.id)
            .where(PrintQueueItem.queue_id == queue_row.id)
            .where(PrintQueueItem.status == "printing")
        )
    ).scalar_one_or_none()
    if real_printing is not None:
        return None

    # Look up the active archive for this printer.
    from backend.app.main import _active_prints  # lazy to avoid cycle

    archive_id: int | None = None
    for (pid, _fname), aid in _active_prints.items():
        if pid == printer_id:
            archive_id = aid
            break

    if archive_id is None:
        return None

    archive = (await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))).scalar_one_or_none()
    if archive is None:
        return None

    # Source detection.  BamDude-dispatched prints always populate
    # ``_active_prints`` before reaching on_print_start (via
    # ``_expected_prints`` registration).  We don't have a durable
    # "dispatched by us" marker, so we fall back to a conservative
    # default: ``external``.  Known direct-dispatch path sets a flag
    # on the archive via ``register_expected_print``; check that too.
    source = "external"
    extra = archive.extra_data or {}
    if extra.get("_dispatched_by_bamdude"):
        source = "bamdude_direct"

    started_at = archive.started_at or datetime.now(timezone.utc)

    return {
        "id": -printer_id,  # negative sentinel, never collides with real ids
        "queue_id": queue_row.id,
        "printer_id": printer_id,
        "waiting_reason": None,
        "archive_id": archive.id,
        "library_file_id": None,
        "position": -1,
        "scheduled_time": None,
        "auto_off_after": False,
        "manual_start": False,
        "ams_mapping": None,
        "plate_id": archive.extra_data.get("plate_id") if archive.extra_data else None,
        "bed_levelling": True,
        "flow_cali": False,
        "layer_inspect": False,
        "timelapse": False,
        "use_ams": True,
        "mesh_mode_fast_check": True,
        "execute_swap_macros": False,
        "swap_macro_events": None,
        "status": "printing",
        "started_at": started_at,
        "completed_at": None,
        "error_message": None,
        "created_at": archive.created_at,
        "batch_id": None,
        "archive_name": archive.print_name or archive.filename,
        "archive_thumbnail": archive.thumbnail_path,
        "library_file_name": None,
        "library_file_thumbnail": None,
        "printer_name": printer_manager.get_printer(printer_id).name
        if printer_manager.get_printer(printer_id)
        else None,
        "print_time_seconds": archive.print_time_seconds,
        "filament_used_grams": archive.filament_used_grams,
        "filament_type": archive.filament_type,
        "filament_color": archive.filament_color,
        "layer_height": archive.layer_height,
        "nozzle_diameter": archive.nozzle_diameter,
        "sliced_for_model": archive.sliced_for_model,
        "created_by_id": archive.created_by_id,
        "created_by_username": None,
        # Virtual-item extensions:
        "is_virtual": True,
        "source": source,
    }
