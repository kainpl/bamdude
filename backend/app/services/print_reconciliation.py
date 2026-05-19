"""Startup print reconciliation.

When BamDude is stopped and a print finishes on the printer during the
downtime, the restarted process never sees the live ``RUNNING -> FINISH``
MQTT transition that drives ``on_print_complete``, so the ``PrintArchive``
row stays ``status='printing'`` forever and the linked ``PrintQueueItem``
never advances.

This service runs once per printer, on the first full MQTT status after a
fresh connect (a stale-watchdog reconnect is already covered by
``BambuMQTTClient.carry_print_lifecycle_from``). It closes orphan
``printing`` archives against the printer's real state.

See ``docs/superpowers/specs/2026-05-19-startup-print-reconciliation-design.md``.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem

logger = logging.getLogger(__name__)

# Printer gcode_state values that mean a print is still in progress — an
# orphan archive matching one of these is left untouched (the live status
# already re-armed the fresh client's completion tracking).
_ACTIVE_STATES = frozenset({"RUNNING", "PAUSE"})


def _file_matches(archive_filename: str, live_file: str) -> bool:
    """True when the archive's print file is the printer's current file.

    Tolerant of path prefixes and extension differences — the printer's
    ``gcode_file`` may arrive as ``ftp:///cache/name.gcode.3mf`` while the
    archive stores ``name.3mf``. Compares the lowercased basename with all
    trailing extensions stripped.
    """
    if not archive_filename or not live_file:
        return False

    def _stem(name: str) -> str:
        base = os.path.basename(name.strip().replace("\\", "/").rstrip("/"))
        while "." in base:
            base = base.rsplit(".", 1)[0]
        return base.lower()

    a, b = _stem(archive_filename), _stem(live_file)
    return bool(a) and a == b


def _classify(live_state: str, *, file_match: bool) -> str:
    """Decide what to do with one orphan ``printing`` archive.

    Returns one of:

    - ``"running"`` — printer is still printing this file; no-op.
    - ``"completed"`` — printer finished it; close as completed.
    - ``"failed"`` — printer reports a failure; close as failed.
    - ``"uncertain"`` — printer moved on to a different/unknown file, so
      the real outcome is unknowable; close as completed but flagged.
    """
    if not file_match:
        return "uncertain"
    if live_state in _ACTIVE_STATES:
        return "running"
    if live_state == "FAILED":
        return "failed"
    # FINISH / IDLE / anything else with a file match — trust the printer
    # state: the job is done.
    return "completed"


def _slicer_estimates(file_path: str) -> dict:
    """Best-effort slicer estimates from a 3MF, for a recovered print.

    Returns ``{"print_time_seconds": int, "filament_used_grams": float}``
    with only the keys it could read. Any failure (no file, not a 3MF,
    parse error) returns ``{}`` — the MQTT completion event that would
    have carried the real figures is gone, so estimates are a courtesy,
    never a hard requirement.
    """
    if not file_path or not os.path.isfile(file_path):
        return {}
    try:
        from backend.app.services.archive import ThreeMFParser

        meta = ThreeMFParser(Path(file_path)).parse()
        out: dict = {}
        if isinstance(meta, dict):
            pts = meta.get("print_time_seconds")
            fug = meta.get("filament_used_grams")
            if isinstance(pts, (int, float)) and pts > 0:
                out["print_time_seconds"] = int(pts)
            if isinstance(fug, (int, float)) and fug > 0:
                out["filament_used_grams"] = float(fug)
        return out
    except Exception as exc:  # noqa: BLE001 — best-effort, never fatal
        logger.debug("reconcile: slicer-estimate parse failed for %s — %s", file_path, exc)
        return {}


async def _reconcile_complete_archive(
    db: AsyncSession,
    archive: PrintArchive,
    *,
    status: str,
    uncertain: bool,
) -> None:
    """Close one orphan ``printing`` archive and advance its queue.

    ``status`` is ``"completed"`` or ``"failed"``. ``uncertain`` records
    that the printer had moved on, so the outcome could not be verified.
    Best-effort slicer estimates fill telemetry fields only when they are
    still ``NULL`` — never overwrites a real value. Energy is left
    untouched: a smart-plug reading taken now would over-count the idle
    draw since the print actually finished.

    The caller commits.
    """
    from backend.app.services.queue_counters import set_queue_error, set_queue_idle, update_queue_counters

    now = datetime.now(timezone.utc)
    archive.status = status
    archive.completed_at = now

    # Best-effort telemetry — only fill what is missing.
    estimates = _slicer_estimates(archive.file_path or "")
    if archive.print_time_seconds is None and "print_time_seconds" in estimates:
        archive.print_time_seconds = estimates["print_time_seconds"]
    if archive.filament_used_grams is None and "filament_used_grams" in estimates:
        archive.filament_used_grams = estimates["filament_used_grams"]

    # Audit flags — reassign the dict so SQLAlchemy flags the JSON column dirty.
    extra = dict(archive.extra_data or {})
    extra["recovered_by_startup_sweep"] = True
    if uncertain:
        extra["recovered_outcome_uncertain"] = True
    archive.extra_data = extra

    # Advance the linked queue item, if any.
    item = (
        await db.execute(select(PrintQueueItem).where(PrintQueueItem.archive_id == archive.id))
    ).scalar_one_or_none()
    if item is not None:
        item.status = status
        item.completed_at = now
        if status == "failed":
            await set_queue_error(db, item.queue_id, failed_item_id=item.id)
        else:
            await set_queue_idle(db, item.queue_id)
        await update_queue_counters(db, item.queue_id)

    # Arm the plate-clear gate unconditionally — the recovered print's
    # plate is physically still on the bed after an unsupervised gap, so
    # the next job must wait for the operator's clear-plate confirmation.
    if archive.printer_id is not None:
        from backend.app.services.printer_manager import printer_manager

        printer_manager.set_awaiting_plate_clear(archive.printer_id, True)

    logger.info(
        "reconcile: closed archive %s as %s%s",
        archive.id,
        status,
        " (outcome uncertain)" if uncertain else "",
    )


async def _reconcile(db: AsyncSession, printer_id: int, live_state: str, live_file: str) -> None:
    """Reconcile every orphan ``printing`` archive for one printer.

    Takes an explicit session so tests can drive it directly;
    :func:`reconcile_printer_prints` is the production wrapper.
    """
    orphans = list(
        (
            await db.execute(
                select(PrintArchive)
                .where(PrintArchive.printer_id == printer_id)
                .where(PrintArchive.status == "printing")
            )
        ).scalars()
    )
    if not orphans:
        return

    closed = 0
    for archive in orphans:
        action = _classify(live_state, file_match=_file_matches(archive.filename or "", live_file))
        if action == "running":
            continue  # still printing — the live RUNNING status self-arms completion
        if action == "failed":
            await _reconcile_complete_archive(db, archive, status="failed", uncertain=False)
        elif action == "uncertain":
            await _reconcile_complete_archive(db, archive, status="completed", uncertain=True)
        else:  # completed
            await _reconcile_complete_archive(db, archive, status="completed", uncertain=False)
        closed += 1

    if closed:
        logger.info("reconcile: closed %d orphan print(s) on startup for printer %d", closed, printer_id)


async def reconcile_printer_prints(printer_id: int, live_state: str, live_file: str) -> None:
    """Entry point — runs once per printer on the first full MQTT status
    after a fresh start. Opens its own session and commits."""
    from backend.app.core.database import async_session

    try:
        async with async_session() as db:
            await _reconcile(db, printer_id, live_state, live_file)
            await db.commit()
    except Exception:  # noqa: BLE001 — a background sweep must never crash the connect path
        logger.exception("reconcile: startup sweep failed for printer %d", printer_id)
