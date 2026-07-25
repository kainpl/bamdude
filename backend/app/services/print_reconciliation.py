"""Connect-edge print reconciliation.

When BamDude is stopped (or disconnected from a printer) and a print finishes
during the gap, the reconnected process never sees the live ``RUNNING ->
FINISH`` MQTT transition that drives ``on_print_complete``, so the
``PrintArchive`` row stays ``status='printing'`` forever and the linked
``PrintQueueItem`` never advances.

This service runs on the first full MQTT status after **each** fresh connect
(re-armed per connect edge, not once per process — #1542 follow-up). It closes
orphan ``printing`` archives against the printer's real state.

Two closure signals, both against a single archive that still says
``printing``:

- **State**: the printer reports the same file but is no longer active
  (FINISH / IDLE / FAILED) — the job is simply done.
- **Subtask replay**: the printer reports the same *file* still RUNNING but
  under a **different** ``subtask_id`` than the archive tracked. That means a
  firmware ghost-replay (or a smart-plug power-cycle auto-restart) reran the
  file under a new subtask after BamDude missed the completion — the tracked
  archive is superseded and closed as outcome-uncertain, rather than left
  wrongly classified as "still running" forever.

See ``docs/superpowers/specs/2026-05-19-startup-print-reconciliation-design.md``.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings
from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.utils.safe_path import PathTraversalError, safe_join_under

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


def _subtask_norm(name: str) -> str:
    """Normalise a subtask / print name for identity comparison.

    Basename, trailing sliced-file extensions stripped (``.gcode.3mf`` /
    ``.gcode`` / ``.3mf``), whitespace trimmed, lowercased. Deliberately does
    NOT strip *every* dot the way :func:`_file_matches` does — subtask names are
    human labels that may legitimately contain dots, and both sides here derive
    from the same ``subtask_name`` so aggressive stripping only risks collisions.
    """
    s = os.path.basename((name or "").strip().replace("\\", "/").rstrip("/"))
    for ext in (".gcode.3mf", ".gcode", ".3mf"):
        if s.lower().endswith(ext):
            s = s[: -len(ext)]
            break
    return s.strip().lower()


def _name_matches_subtask(archive: PrintArchive, live_subtask_name: str) -> bool:
    """True when the archive's print identity matches the printer's current
    ``subtask_name`` — a fallback for firmware that hides the real filename.

    H2/X-series firmware (H2D, H2D Pro, H2C, H2S, X2D, P2S) reports the live
    ``gcode_file`` as a generic internal path — ``/data/Metadata/plate_1.gcode``
    — which never shares a stem with the archive's sliced filename, so
    :func:`_file_matches` yields a false negative. Without this fallback an
    actively-RUNNING print is misclassified as "printer moved on" and closed as
    outcome-uncertain on every reconnect (duplicate archive + false completion).
    The printer still reports the real ``subtask_name``, which BamDude persists
    as ``PrintArchive.print_name`` (and the sliced ``filename`` stem) — match on
    that.

    Requires both sides non-empty: an empty live subtask is ambiguous (printer
    between jobs) and must fall through to the state-based classification, never
    force a spurious match.
    """
    live = _subtask_norm(live_subtask_name)
    if not live:
        return False
    return _subtask_norm(archive.print_name or "") == live or _subtask_norm(archive.filename or "") == live


def _classify(live_state: str, *, file_match: bool, subtask_stale: bool = False) -> str:
    """Decide what to do with one orphan ``printing`` archive.

    Returns one of:

    - ``"running"`` — printer is still printing this exact print; no-op.
    - ``"completed"`` — printer finished it; close as completed.
    - ``"failed"`` — printer reports a failure; close as failed.
    - ``"uncertain"`` — printer moved on to a different/unknown file, or is
      running the same file under a **different** subtask_id (a ghost-replay
      superseded the tracked print), so the real outcome is unknowable; close
      as completed but flagged.
    """
    if not file_match:
        return "uncertain"
    # Same file, but a different subtask_id means the printer re-ran it (a
    # firmware ghost-replay / power-cycle auto-restart) after we lost the
    # original completion — the tracked archive is superseded, not "running"
    # (#1542 follow-up).
    if subtask_stale:
        return "uncertain"
    if live_state in _ACTIVE_STATES:
        return "running"
    if live_state == "FAILED":
        return "failed"
    # FINISH / IDLE / anything else with a file match — trust the printer
    # state: the job is done.
    return "completed"


def _slicer_estimates(file_path: str, plate_index: int | None = None) -> dict:
    """Best-effort slicer estimates from a 3MF, for a recovered print.

    Returns ``{"print_time_seconds": int, "filament_used_grams": float}``
    with only the keys it could read. Any failure (no file, not a 3MF,
    parse error) returns ``{}`` — the MQTT completion event that would
    have carried the real figures is gone, so estimates are a courtesy,
    never a hard requirement.

    ``file_path`` is a ``PrintArchive.file_path``, i.e. relative to
    ``settings.base_dir`` (see ``archive.py`` where it is written as
    ``dest_file.relative_to(settings.base_dir)``); it is resolved the same way
    every other reader does. ``plate_index`` scopes the parse to the plate that
    was actually printed — without it a multi-plate container falls through to
    ``root.find(".//plate")`` and reports plate 1's weight and time for a print
    of plate 5.
    """
    if not file_path:
        return {}
    resolved = Path(file_path)
    if not resolved.is_absolute():
        # The value comes from our own writer, but it is still a DB string —
        # join it under base_dir through the traversal guard rather than
        # trusting it. Non-fatal: this whole function is best-effort.
        try:
            resolved = safe_join_under(settings.base_dir, file_path, http=False)
        except PathTraversalError as exc:
            logger.warning("reconcile: refusing unsafe archive path %r — %s", file_path, exc)
            return {}
    if not resolved.is_file():
        return {}
    try:
        from backend.app.services.archive import ThreeMFParser

        meta = ThreeMFParser(resolved, plate_number=plate_index).parse()
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
    if status == "failed" and not archive.failure_reason:
        # Not a user action and not an HMS-classified fault — record why the row
        # was closed, and that its end time is a reconstruction (#2592).
        archive.failure_reason = "Stale - reconciled after reconnect, end time unknown"

    # Best-effort telemetry — only fill what is missing.
    estimates = _slicer_estimates(archive.file_path or "", archive.plate_index)
    if archive.print_time_seconds is None and "print_time_seconds" in estimates:
        archive.print_time_seconds = estimates["print_time_seconds"]
    if archive.filament_used_grams is None and "filament_used_grams" in estimates:
        archive.filament_used_grams = estimates["filament_used_grams"]

    # NOT ``now``: the reconnect moment is not the print's end time, and the gap
    # would be banked as print time by the actual_time_seconds hook and by the
    # /archives/stats total (#2592). Computed AFTER the estimate fill above, so a
    # fallback archive that just learned its print_time_seconds gets the better
    # bound.
    archive.completed_at = _recovered_completed_at(archive.started_at, archive.print_time_seconds, now)

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


def _recovered_completed_at(
    started_at: datetime | None,
    print_time_seconds: int | None,
    now: datetime,
) -> datetime:
    """Honest ``completed_at`` for a reconciled (synthetic) closure.

    The real end time is unknown: the print stopped somewhere inside the
    disconnect window, and ``now`` is only the reconnect moment. Stamping
    ``now`` banks the entire gap as print time everywhere a duration is derived
    from ``completed_at - started_at`` — the ``before_flush`` hook that fills
    ``PrintArchive.actual_time_seconds`` (``core/database.py``) and the
    ``GET /archives/stats`` total. Across a farm of stale rows that is hundreds
    of fictitious hours (upstream #2592).

    Rules, mirroring ``main.py::_close_stale_printing_rows``:

    * slicer estimate known → the printer's own predicted natural end,
      ``started_at + print_time_seconds``, clamped to ``now`` so a
      short-downtime reconcile can never stamp a finish in the future;
    * no estimate → ``started_at``, i.e. zero measured runtime. Upstream stores
      ``duration_seconds=0`` for the same reason: with no evidence at all,
      contributing nothing is the honest answer;
    * no ``started_at`` → ``now``; there is no gap to bank, and both the
      metrics hook and the stats total already skip such rows.
    """
    if started_at is None:
        return now
    started = started_at if started_at.tzinfo else started_at.replace(tzinfo=timezone.utc)
    if print_time_seconds and print_time_seconds > 0:
        return min(now, started + timedelta(seconds=print_time_seconds))
    return started


def _subtask_stale(archive_subtask_id: str | None, live_subtask_id: str) -> bool:
    """True when the archive tracked a different subtask than the printer is
    now running for the same file — the ghost-replay signal.

    Requires BOTH ids to be known and to differ: a missing id on either side
    is ambiguous (older archive, or the printer hasn't reported a subtask
    between jobs), so we fall back to the state-based classification instead
    of forcing a close.
    """
    a = (archive_subtask_id or "").strip()
    b = (live_subtask_id or "").strip()
    return bool(a) and bool(b) and a != b


async def _reconcile(
    db: AsyncSession,
    printer_id: int,
    live_state: str,
    live_file: str,
    live_subtask_id: str = "",
    live_subtask_name: str = "",
) -> None:
    """Reconcile every orphan ``printing`` archive for one printer.

    Takes an explicit session so tests can drive it directly;
    :func:`reconcile_printer_prints` is the production wrapper.
    """
    # Pre-push_status guard (upstream #1679 parity). On the bare-connect edge
    # the printer's live state is on construction defaults ("" / "unknown") —
    # MQTT connected but the first real ``push_status`` hasn't applied yet. That
    # degenerate state is NOT evidence a print ended; closing orphans against it
    # would synthesise bogus completions (and, downstream, double-counted
    # filament). Our production trigger (``on_first_status``) only fires once a
    # real ``gcode_state`` has arrived, so in normal operation this is
    # unreachable; it's belt-and-braces for any future caller that reconciles
    # earlier in the connect sequence.
    if (live_state or "").upper() in ("", "UNKNOWN"):
        return

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
        # File-name match (P1S/A1/etc.) OR subtask-name match (H2/X-series report
        # a generic ``/data/Metadata/plate_N.gcode`` that never matches the
        # sliced filename — see :func:`_name_matches_subtask`).
        file_match = _file_matches(archive.filename or "", live_file) or _name_matches_subtask(
            archive, live_subtask_name
        )
        action = _classify(
            live_state,
            file_match=file_match,
            subtask_stale=file_match and _subtask_stale(archive.subtask_id, live_subtask_id),
        )
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


async def reconcile_printer_prints(
    printer_id: int,
    live_state: str,
    live_file: str,
    live_subtask_id: str = "",
    live_subtask_name: str = "",
) -> None:
    """Entry point — runs on the first full MQTT status after each fresh
    connect. Opens its own session and commits."""
    from backend.app.core.database import async_session

    try:
        async with async_session() as db:
            await _reconcile(db, printer_id, live_state, live_file, live_subtask_id, live_subtask_name)
            await db.commit()
    except Exception:  # noqa: BLE001 — a background sweep must never crash the connect path
        logger.exception("reconcile: connect-edge sweep failed for printer %d", printer_id)
