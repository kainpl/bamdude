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

# How long after its row was created an archive is still considered "a job on
# its way to the printer" rather than an orphan. Covers the whole dispatch
# pipeline plus the printer's own run-up: on a swap farm the gap between the
# archive being created and the print actually starting was measured at ~1m50s
# (plate change, then heat-up). Matches ``PrintScheduler._dispatch_max_hold``.
#
# ⚠️ This is the floor, not the whole window. With the preheat stage enabled the
# dispatcher holds the printer at temperature between the upload and
# ``start_print`` — by default up to 20 minutes, and up to 90 at the settings'
# limits — so a fixed 180 would call a job that is heating an orphan and close
# it. ``_grace_seconds`` adds the stage's own worst case when it applies.
_JUST_DISPATCHED_SECONDS = 180


async def _grace_seconds(db: AsyncSession, archive: PrintArchive) -> int:
    """The window for THIS archive: the base, plus a preheat stage if it has one.

    ⚠️ Asked per archive, not once per printer, because preheat is decided per
    print: ``PrintQueueItem.preheat_override`` can force the stage on while the
    global setting is off, and off while it is on. The queue item is reachable
    because the dispatcher wires ``archive_id`` in the same transaction that
    creates the row.

    A print with no queue item — external, or started from the printer's screen —
    has no override to read and falls through to the global setting. That is the
    right answer for the wrong-looking reason: BamDude does not preheat for a
    print it did not dispatch, but it also cannot be mid-dispatch on one, so the
    extra grace costs nothing.

    Best-effort: any failure here returns the base window rather than raising.
    Getting this number wrong delays a cleanup; raising would abandon the sweep.
    """
    from backend.app.services.preheat import planned_stage_seconds

    try:
        override = await db.scalar(
            select(PrintQueueItem.preheat_override).where(PrintQueueItem.archive_id == archive.id)
        )
        return _JUST_DISPATCHED_SECONDS + await planned_stage_seconds(db, override=override or "inherit")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("reconcile: could not size the dispatch window for archive %d (%s)", archive.id, exc)
        return _JUST_DISPATCHED_SECONDS


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

    ⚠️ **Spaces and underscores are folded together, because the printer folds
    them.** The firmware echoes ``subtask_name`` as the *sanitised* file stem,
    with every space turned into an underscore, while the archive keeps the name
    as uploaded. One space was enough to lose a print: an X2D mid-job reported

        subtask: AMS_2_Pro_Dry_Pods_–_Modular_Desiccant_System_Rear_Dry_Pod

    against an archive filename ending ``…_Rear Dry Pod.gcode.3mf``. Identical
    but for that, the fallback in :func:`_name_matches_subtask` missed, and
    because H2/X-series firmware also hides the real filename there was nothing
    left to match on — a print two hours in was closed as completed on restart.
    Four A1 Minis printing the same evening were untouched: their filename
    happens to contain no spaces at all.

    Folding is the safe direction. It can only make a match *more* likely, and
    on a false positive :func:`_classify` answers "running" (keep the row) or
    "completed" rather than "uncertain" — none of which invents a completion for
    a print that is still going.
    """
    s = os.path.basename((name or "").strip().replace("\\", "/").rstrip("/"))
    for ext in (".gcode.3mf", ".gcode", ".3mf"):
        if s.lower().endswith(ext):
            s = s[: -len(ext)]
            break
    return s.strip().lower().replace(" ", "_")


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

    ⚠️ **``filename`` is the candidate that actually carries the weight**;
    ``print_name`` is checked first only because it is the cheaper identity when
    it happens to agree. On a multi-plate job ``print_name`` gains a
    ``" - Plate N"`` suffix and can never equal a subtask by construction — the
    archive that started this whole investigation read *"AMS 2 Pro Dry Pods –
    Modular Desiccant System - Plate 5"*. Do not "fix" that by stripping the
    suffix: a file may legitimately be named that way, and the filename match
    (see :func:`_subtask_norm` on space folding) already answers it correctly.
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
) -> int:
    """Close one orphan ``printing`` archive and advance its queue.

    ``status`` is ``"completed"`` or ``"failed"``. ``uncertain`` records
    that the printer had moved on, so the outcome could not be verified.
    Best-effort slicer estimates fill telemetry fields only when they are
    still ``NULL`` — never overwrites a real value.

    A print that ended while the process was down is still a finished print,
    so the completion bookkeeping happens here too: the library counters are
    bumped exactly as the live handler does, and the archive id is returned so
    the caller can recover the energy figure after it commits.

    Energy is deliberately NOT read here. It needs a live round-trip to the
    plug, which has no business inside the sweep's transaction, and it is only
    meaningful once this row is committed.

    Returns the archive id. The caller commits.
    """
    from backend.app.main import _bump_library_file_usage
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

    # Successes only, matching the live handler: a file attempted three times
    # and failed three times has a print_count of 0.
    if status == "completed":
        await _bump_library_file_usage(db, archive.library_file_id)

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
    return archive.id


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
) -> list[int]:
    """Reconcile every orphan ``printing`` archive for one printer.

    Returns the ids of the archives it closed, for the caller's post-commit
    energy recovery. Takes an explicit session so tests can drive it directly;
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
        return []

    # A dispatch in flight is not an orphan.
    #
    # The sweep re-arms on every client recreation (#1542) so a print that ended
    # during a disconnect still gets closed. That is right — but a stale-MQTT
    # reconnect can land in the seconds between "archive created for the job we
    # just sent" and "printer actually starts printing it". In that window the
    # printer is still reporting the PREVIOUS job's FINISH, and on a farm that
    # reprints one file the filename matches, so ``_classify`` calls a job that
    # has not begun "completed" and closes it — taking its queue item with it.
    #
    # Seen on an operator's swap farm whose MQTT went stale every ~45 minutes,
    # almost exactly its print cycle: every dispatch that collided with a
    # reconnect lost its queue item seconds after dispatch, while the printer
    # went on to print the file for the full 43 minutes. From the outside the
    # queue emptied itself without producing parts.
    #
    # The re-arm's own justification — "re-running is idempotent, a print still
    # RUNNING classifies as running and no-ops" — holds only once the print has
    # *started*. This is the case it does not cover.
    from backend.app.services.print_scheduler import scheduler as print_scheduler

    if print_scheduler.has_dispatch_in_flight(printer_id):
        logger.info(
            "reconcile: printer %d was handed a job that has not started yet — leaving its archives alone",
            printer_id,
        )
        return []

    # The dispatch hold above only exists once the print command has *landed*
    # (``_mark_printer_dispatched`` runs after a successful dispatch), and the
    # collision happens earlier than that — the dispatcher calls
    # ``ensure_fresh_connection_for_printer`` before uploading, so a stale link
    # reconnects, re-arms this sweep, and the first push arrives while the FTP
    # upload is still going. In the operator's log the sweep closed the archive
    # 0.9 s after it was created and 1m44s before its print began.
    #
    # ``created_at`` is the signal that covers that window: it is stamped when
    # the row is inserted and never moves, unlike ``started_at``, which is only
    # filled once the printer really starts.
    now = datetime.now(timezone.utc)
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
        return []

    closed = 0
    recovered: list[int] = []
    for archive in orphans:
        created = archive.created_at
        if created is not None:
            created = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
            grace = await _grace_seconds(db, archive)
            if (now - created).total_seconds() < grace:
                logger.info(
                    "reconcile: archive %d on printer %d was created %.0fs ago (window %ds) — a job that "
                    "has not started yet is not an orphan, leaving it alone",
                    archive.id,
                    printer_id,
                    (now - created).total_seconds(),
                    grace,
                )
                continue

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
            recovered.append(await _reconcile_complete_archive(db, archive, status="failed", uncertain=False))
        elif action == "uncertain":
            recovered.append(await _reconcile_complete_archive(db, archive, status="completed", uncertain=True))
        else:  # completed
            recovered.append(await _reconcile_complete_archive(db, archive, status="completed", uncertain=False))
        closed += 1

    if closed:
        logger.info("reconcile: closed %d orphan print(s) on startup for printer %d", closed, printer_id)
    return recovered


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

    recovered: list[int] = []
    try:
        async with async_session() as db:
            recovered = await _reconcile(db, printer_id, live_state, live_file, live_subtask_id, live_subtask_name)
            await db.commit()
    except Exception:  # noqa: BLE001 — a background sweep must never crash the connect path
        logger.exception("reconcile: connect-edge sweep failed for printer %d", printer_id)

    # Energy for a print that ended while we were down. Spawned rather than
    # awaited, and only after the commit: the reading is a live round-trip to
    # the plug (a Zigbee radio read takes seconds) and this runs on the connect
    # path. Same call the live handler makes, so there is one implementation of
    # "counter now, minus what was banked at start".
    #
    # The figure is marked approximate because it is: the counter has kept
    # climbing since the print really ended, so the printer's idle draw over
    # the gap is folded in. That is a known over-count on the order of tens of
    # watts, against a print measured in hundreds — and it is worth having,
    # because the alternative is an archive that reads as though the print
    # used no power at all.
    for archive_id in recovered:
        from backend.app.core.tasks import spawn_background_task
        from backend.app.main import _record_print_energy

        spawn_background_task(
            _record_print_energy(archive_id, printer_id, approximate=True),
            name=f"reconcile-energy-{archive_id}",
        )

    await _load_objects_for_a_print_already_running(printer_id, live_state)


async def _load_objects_for_a_print_already_running(printer_id: int, live_state: str) -> None:
    """Fill ``printable_objects`` for a print that was under way before we got here.

    ⚠️ **Nothing else does this.** The three writers of
    ``state.skip_objects_supported`` are two branches of ``GET /print/objects``
    and ``on_print_start`` — and ``on_print_start`` fires on a *transition* into
    printing. A backend that starts, or an MQTT client that is recreated, while
    a plate is half done sees no such transition: it joins mid-print. A comment
    in ``main.py`` claimed a restart "papers over the symptom, the next
    on_print_start takes the full path" — there is no next one.

    What that cost, measured on a live farm: after a restart the Skip Objects
    button was dark on 3 of 4 machines printing the *same file*, and stayed
    dark until somebody opened the dialog in the web — which is the one action
    that calls the route that loads. The operator on a phone had no way to
    reach it at all.

    Runs on the connect edge, reads one archive row and one 3MF off local disk,
    and is wrapped so a failure can never touch the connect path.
    ``is_retrigger=True``: this is the print already in progress, so an
    unreadable file must leave state alone rather than blank it.
    """
    if live_state not in ("RUNNING", "PAUSE"):
        return

    try:
        from backend.app.core.database import async_session
        from backend.app.services.archive import load_objects_from_archive_into_state

        async with async_session() as db:
            archive = (
                (
                    await db.execute(
                        select(PrintArchive)
                        .where(
                            PrintArchive.printer_id == printer_id,
                            PrintArchive.status == "printing",
                            PrintArchive.file_path != "",
                        )
                        .order_by(PrintArchive.id.desc())
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )

        if archive is None:
            # No archive yet (the 3MF is still downloading) or the print ended
            # and the sweep above just closed it. Either way there is nothing
            # to read, and the download path loads objects when it lands.
            return

        if load_objects_from_archive_into_state(archive, printer_id, is_retrigger=True):
            logger.info(
                "reconcile: loaded printable objects for printer %d from archive %d (joined mid-print)",
                printer_id,
                archive.id,
            )
    except Exception:  # noqa: BLE001 — a convenience load must never break connecting
        logger.exception("reconcile: mid-print object load failed for printer %d", printer_id)


async def release_interrupted_dispatch_claims(db: AsyncSession) -> int:
    """Give back printers claimed by a dispatch that died before it archived anything.

    ``_start_print`` commits the claim — the item to ``printing`` and
    ``PrinterQueue.status='printing'`` — and only then spawns the FTP pipeline.
    The dispatcher creates the archive *before* the upload starts, so a process
    that dies between those two points leaves a claim with no archive behind it,
    and nothing was ever sent to the printer.

    Nothing reclaimed that. Every other release path needs evidence this case
    does not produce: :func:`_reconcile` selects ``PrintArchive.status=='printing'``
    and finds none, the ``on_print_*`` handlers need a completion event for a
    print that never started, and the in-process bail-out died with the process.
    ``check_queue`` then seeds ``busy_printers`` from a bare
    ``status='printing'`` select with no age check and no cross-check against the
    printer, so the claim reads as live for ever. m120's docstring names the
    outcome: "claims the printer forever and the farm quietly stops taking work".

    **Startup only, and it must run before the scheduler's first tick.** No age
    threshold is needed or wanted: at startup everything in flight is dead by
    definition, because the process that owned it is gone. Running this while the
    dispatcher is live would race the very window it exists to clean up.

    m120 refused to repair these rows because "a stale ``printing`` row and a live
    one are the same row". They are not, at startup, given the right evidence —
    but the discriminator has to be evidence and not a heuristic, so a claim is
    released only when all three hold:

    1. the queue names the item holding the claim. An external or direct print
       claims with ``current_item_id=None``; its truth lives in MQTT, not in our
       tables, and we have nothing to prove here — left alone.
    2. that item is still ``printing``.
    3. **the printer has no archive in ``printing``.** This is the load-bearing
       one. An archive means the dispatcher got past its own creation, so the
       print may be running; releasing then would double-dispatch onto a busy
       printer, which is the failure the claim exists to prevent.

    Returns the number of claims released, and does not commit — the caller owns
    the transaction.
    """
    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.services.queue_counters import set_queue_idle, update_queue_counters

    claimed = (
        (
            await db.execute(
                select(PrinterQueue)
                .where(PrinterQueue.status == "printing")
                .where(PrinterQueue.current_item_id.is_not(None))
            )
        )
        .scalars()
        .all()
    )
    if not claimed:
        return 0

    released = 0
    for queue in claimed:
        item = (
            await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == queue.current_item_id))
        ).scalar_one_or_none()
        if item is None or item.status != "printing":
            continue

        live_archive = (
            await db.execute(
                select(PrintArchive.id)
                .where(PrintArchive.printer_id == queue.printer_id)
                .where(PrintArchive.status == "printing")
                .limit(1)
            )
        ).scalar_one_or_none()
        if live_archive is not None:
            continue

        logger.warning(
            "Startup: queue %s claimed printer %s for item %s, but no archive was ever created — "
            "the dispatch died before anything reached the printer. Releasing the claim and "
            "returning the item to pending.",
            queue.id,
            queue.printer_id,
            item.id,
        )
        item.status = "pending"
        item.started_at = None
        item.error_message = None
        await set_queue_idle(db, queue.id)
        await update_queue_counters(db, queue.id)
        released += 1

    return released
