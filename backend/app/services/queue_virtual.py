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
import time
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
_SQL_CHUNK = 400  # portable below SQLite's conservative 999 bind limit
_anomaly_logged_at: dict[int, float] = {}


def _chunks(values: list[int]):
    for start in range(0, len(values), _SQL_CHUNK):
        yield values[start : start + _SQL_CHUNK]


def _live_identity(printer_id: int):
    """Capture scalar device state and immutable run binding across DB awaits."""
    from backend.app.services.print_run_binding import current_print_run

    state = printer_manager.get_status(printer_id)
    if state is None or not state.connected or state.state not in _ACTIVE_STATES:
        return None
    return (
        current_print_run(printer_manager, printer_id),
        state.state,
        getattr(state, "subtask_id", None),
        getattr(state, "gcode_file", None),
    )


async def build_virtual_current_prints(
    db: AsyncSession,
    *,
    queue_id: int | None = None,
    viewer_id: int | None = None,
) -> list[dict[str, Any]]:
    """Build virtual rows with bounded scalar stages, regardless of farm size."""
    queues_stmt = select(PrinterQueue.id, PrinterQueue.printer_id)
    if queue_id is not None:
        queues_stmt = queues_stmt.where(PrinterQueue.id == queue_id)
    queue_rows = (await db.execute(queues_stmt)).all()
    active = []
    for q_id, printer_id in queue_rows:
        identity = _live_identity(printer_id)
        if identity is not None:
            active.append((q_id, printer_id, identity))
    if not active:
        return []

    real_by_queue: dict[int, list[int]] = {}
    for ids in _chunks([q_id for q_id, _, _ in active]):
        rows = await db.execute(
            select(PrintQueueItem.queue_id, PrintQueueItem.id).where(
                PrintQueueItem.queue_id.in_(ids), PrintQueueItem.status == "printing"
            )
        )
        for q_id, item_id in rows.all():
            real_by_queue.setdefault(q_id, []).append(item_id)

    from backend.app.main import _active_prints  # lazy to avoid cycle

    legacy_by_printer: dict[int, tuple[tuple[int, str], int]] = {}
    for alias, archive_id in _active_prints.items():
        legacy_by_printer.setdefault(alias[0], (alias, archive_id))

    candidates = []
    for q_id, printer_id, identity in active:
        real_ids = real_by_queue.get(q_id, [])
        if real_ids:
            if len(real_ids) > 1:
                now = time.monotonic()
                if now - _anomaly_logged_at.get(q_id, 0) >= 300:
                    if len(_anomaly_logged_at) >= 256:
                        _anomaly_logged_at.clear()
                    _anomaly_logged_at[q_id] = now
                    logger.warning(
                        "Queue %s (printer %s) has %d 'printing' rows: %s", q_id, printer_id, len(real_ids), real_ids
                    )
            continue
        bound = identity[0]
        alias = None if bound is not None and bound.archive_id is not None else legacy_by_printer.get(printer_id)
        archive_id = (
            bound.archive_id if bound is not None and bound.archive_id is not None else alias[1] if alias else None
        )
        if archive_id is not None:
            candidates.append((q_id, printer_id, identity, alias, archive_id))
    if not candidates:
        return []

    archives: dict[int, PrintArchive] = {}
    for ids in _chunks(sorted({archive_id for *_, archive_id in candidates})):
        rows = await db.execute(select(PrintArchive).where(PrintArchive.id.in_(ids)))
        archives.update({archive.id: archive for archive in rows.scalars().all()})

    virtual = []
    for q_id, printer_id, identity, alias, archive_id in candidates:
        archive = archives.get(archive_id)
        if archive is None or (viewer_id is not None and archive.created_by_id != viewer_id):
            continue
        if _live_identity(printer_id) != identity:
            continue
        if alias is not None and _active_prints.get(alias[0]) != alias[1]:
            continue
        virtual.append(_virtual_response(printer_id, q_id, archive))
    return sorted(virtual, key=lambda row: row["printer_id"])


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
      * Neither the manager-owned live binding nor legacy ``_active_prints``
        aliases identify an archive for this printer.
    """
    queue_id = (
        await db.execute(select(PrinterQueue.id).where(PrinterQueue.printer_id == printer_id))
    ).scalar_one_or_none()
    if queue_id is None:
        return None
    rows = await build_virtual_current_prints(db, queue_id=queue_id)
    return rows[0] if rows else None


def _virtual_response(printer_id: int, queue_id: int, archive: PrintArchive) -> dict[str, Any]:
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
        "queue_id": queue_id,
        "printer_id": printer_id,
        # m173: a synthesised row has no queue source and never will — there is
        # no job here to hydrate. Stated rather than left to the schema default,
        # which would report this running print as ``legacy`` (spec §2, §8).
        "source_storage": "exempt",
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
