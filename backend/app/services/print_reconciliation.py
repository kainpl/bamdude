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
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings
from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.utils.filename import derive_remote_filename
from backend.app.utils.safe_path import PathTraversalError, safe_join_under

logger = logging.getLogger(__name__)

# Recovery must not mistake the normal archive→queue completion window for
# historical damage. A counter also covers overlapping callbacks; no awaits
# occur when entering/leaving this event-loop-owned scope.
_live_completions: dict[int, int] = {}


@contextmanager
def defer_queue_repair_during_completion(printer_id: int):
    _live_completions[printer_id] = _live_completions.get(printer_id, 0) + 1
    try:
        yield
    finally:
        remaining = _live_completions[printer_id] - 1
        if remaining:
            _live_completions[printer_id] = remaining
        else:
            _live_completions.pop(printer_id)


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


def _legacy_dispatched_subtask(filename: str) -> str:
    """Reconstruct the pre-0.6.1 wire label for one archived filename.

    This is intentionally a compatibility *candidate*, not a normalizer.  The
    old MQTT sender applied global case-sensitive replacements after deriving
    the SD-card name.  Replaying it lets a legacy run close itself, while the
    caller still rejects ambiguity between two rows that yield the same label.
    """
    remote = derive_remote_filename(os.path.basename((filename or "").replace("\\", "/")))
    return remote.replace(".3mf", "").replace(".gcode", "")


def _name_matches_subtask_exact(archive: PrintArchive, live_subtask_name: str) -> bool:
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

    Requires both sides to have SAID something: an empty live subtask is
    ambiguous (printer between jobs) and must fall through to the state-based
    classification, never force a spurious match; an archive that recorded no
    name offers nothing to agree with.

    ⚠️ **Emptiness is decided on the raw string, not the normalised one.** A file
    called only ``.gcode.3mf`` (farm, 2026-09-06) goes up as ``/.3mf`` and is
    echoed as ``.3mf``; both normalise to ``""``. Reading that as "printer
    between jobs" refused a live print's own completion — the queue row stayed
    in ``printing`` for good, and one reconnect during the seven-hour job would
    have closed it as completed. Two extension-only names are one file, not none.

    ⚠️ **``filename`` is the candidate that actually carries the weight**;
    ``print_name`` is checked first only because it is the cheaper identity when
    it happens to agree. On a multi-plate job ``print_name`` gains a
    ``" - Plate N"`` suffix and can never equal a subtask by construction — the
    archive that started this whole investigation read *"AMS 2 Pro Dry Pods –
    Modular Desiccant System - Plate 5"*. Do not "fix" that by stripping the
    suffix: a file may legitimately be named that way, and the filename match
    (see :func:`_subtask_norm` on space folding) already answers it correctly.
    """
    if not (live_subtask_name or "").strip():
        return False
    live = _subtask_norm(live_subtask_name)
    return any(
        _norm_names_match(_subtask_norm(candidate), live)
        for candidate in (archive.print_name, archive.filename)
        if (candidate or "").strip()
    )


def _name_matches_subtask_legacy(archive: PrintArchive, live_subtask_name: str) -> bool:
    """Whether one archive matches the former, lossy MQTT sender label.

    This must stay separate from the normal matcher: a legacy label can be
    shared by more than one archived filename, and the reconciliation sweep
    must leave that collision untouched instead of closing arbitrary runs.
    """
    if not (live_subtask_name or "").strip():
        return False
    live = _subtask_norm(live_subtask_name)
    if (archive.filename or "").strip():
        return _norm_names_match(_subtask_norm(_legacy_dispatched_subtask(archive.filename)), live)
    return False


def _name_matches_subtask(archive: PrintArchive, live_subtask_name: str) -> bool:
    """True for either the canonical or one legacy subtask representation."""
    return _name_matches_subtask_exact(archive, live_subtask_name) or _name_matches_subtask_legacy(
        archive, live_subtask_name
    )


# How the printer marks a name it had to cut short. Observed by upstream on real
# hardware at ~100 characters, but the cut-off is not a fixed count — a name with
# multibyte characters came back at 98 — so match the marker, never a length.
_TRUNCATION_MARKER = "..."


def _norm_names_match(candidate: str, live: str) -> bool:
    """Whether two already-normalised names describe the same print.

    Equality is the normal case; :func:`_subtask_norm` has folded away the
    space/underscore substitution and the sliced-file extension by the time this
    is called.

    ⚠️ **The printer also truncates a long name and marks the cut with `...`.**
    A truncated echo has to count as a match, or every print with a long enough
    name looks like a different print — and downstream that means a queue item
    that is never closed and a printer whose queue silently stops (upstream
    #2829). Either side can be the truncated one: the printer truncates what it
    echoes, and an archive whose own name was recorded from an earlier truncated
    echo carries the marker too.

    ⚠️ Two empty strings are equal here, on purpose. The caller has already
    refused a side that said nothing, so an empty *normalised* name is a name
    made only of the extensions :func:`_subtask_norm` strips — ``.gcode.3mf``
    uploaded as ``.3mf`` and echoed as ``.3mf`` (2026-09-06). Same file.
    """
    if candidate == live:
        return True
    if not candidate or not live:
        return False
    for full, cut in ((candidate, live), (live, candidate)):
        if cut.endswith(_TRUNCATION_MARKER) and full.startswith(cut[: -len(_TRUNCATION_MARKER)]):
            return True
    return False


def _classify(live_state: str, *, file_match: bool, subtask_stale: bool = False) -> str:
    """Decide what to do with one orphan ``printing`` archive.

    Returns one of:

    - ``"running"`` — printer is still printing this exact print; no-op.
    - ``"completed"`` — printer finished it; close as completed.
    - ``"failed"`` — printer reports a failure; close as failed.
    - ``"uncertain"`` — printer moved on to a different/unknown file, or is
      running the same file under a **different** subtask_id (a ghost-replay
      superseded the tracked print), so the real outcome is unknowable; close
      as cancelled, pause the queue, and flag the reason for an operator.
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


def _archive_file_path(file_path: str) -> Path | None:
    """Resolve an archive file safely without opening or parsing it.

    Reconciliation may run on a reconnect path.  The caller hands the result
    to the shared process-worker analysis, rather than parsing the 3MF on the
    server event loop.
    """
    if not file_path:
        return None
    resolved = Path(file_path)
    if not resolved.is_absolute():
        # The value comes from our own writer, but it is still a DB string —
        # join it under base_dir through the traversal guard rather than
        # trusting it. Non-fatal: this whole function is best-effort.
        try:
            resolved = safe_join_under(settings.base_dir, file_path, http=False)
        except PathTraversalError as exc:
            logger.warning("reconcile: refusing unsafe archive path %r — %s", file_path, exc)
            return None
    if not resolved.is_file():
        return None
    return resolved


async def _reconcile_complete_archive(
    db: AsyncSession,
    archive: PrintArchive,
    *,
    status: str,
    uncertain: bool,
) -> int:
    """Close one orphan ``printing`` archive and advance its queue.

    ``status`` is ``"completed"``, ``"failed"`` or ``"cancelled"``.
    ``uncertain`` means the printer moved on and the outcome could not be
    verified; it is closed as cancelled, never as a synthetic success.
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
    from backend.app.services.print_file_analysis import (
        begin_historical_print_file_analysis_finishing,
        discard_print_file_analysis,
        get_print_file_analysis,
    )
    from backend.app.services.printer_manager import printer_manager
    from backend.app.services.queue_counters import (
        set_queue_error,
        set_queue_idle,
        set_queue_paused,
        update_queue_counters,
    )

    analysis = None
    analysis_leased = False
    path = _archive_file_path(archive.file_path or "")
    if archive.printer_id is not None and path is not None:
        analysis_leased = begin_historical_print_file_analysis_finishing(
            printer_manager, archive.printer_id, archive.id, path, archive.plate_index
        )
    try:
        if analysis_leased:
            analysis = await get_print_file_analysis(
                printer_manager, archive.printer_id, archive.id, path, archive.plate_index
            )

        now = datetime.now(timezone.utc)
        archive.status = status
        if status == "failed" and not archive.failure_reason:
            archive.failure_reason = "Stale - reconciled after reconnect, end time unknown"
        elif status == "cancelled" and uncertain and not archive.failure_reason:
            archive.failure_reason = "Outcome uncertain after reconnect; inspect the printer and plate before resuming"

        estimates = analysis.slicer_estimates if analysis is not None else {}
        if archive.print_time_seconds is None and "print_time_seconds" in estimates:
            archive.print_time_seconds = int(estimates["print_time_seconds"])
        if archive.filament_used_grams is None and "filament_used_grams" in estimates:
            archive.filament_used_grams = float(estimates["filament_used_grams"])
        archive.completed_at = _recovered_completed_at(archive.started_at, archive.print_time_seconds, now)

        if status == "completed" and archive.printer_id is not None:
            try:
                from sqlalchemy import func as _func

                from backend.app.models.spool_usage_history import SpoolUsageHistory
                from backend.app.services import usage_tracker

                already_booked = (
                    await db.execute(
                        select(_func.count(SpoolUsageHistory.id)).where(SpoolUsageHistory.archive_id == archive.id)
                    )
                ).scalar()
                if not already_booked:
                    persisted_name = await usage_tracker.get_persisted_print_name(db, archive.printer_id)
                    await usage_tracker.on_print_complete(
                        archive.printer_id,
                        {"status": "completed"},
                        printer_manager,
                        db,
                        archive_id=archive.id,
                        expected_print_name=archive.print_name,
                        file_analysis=analysis,
                        # Reconcile owns an outer transaction.  Its isolated
                        # finishing acquire above is the only allowed analysis
                        # attempt; a no-source/unavailable result falls through
                        # to existing accounting fallback without a hidden
                        # caller-session commit here.
                        analysis_attempted=True,
                        archive_snapshot=archive,
                    )
                    if persisted_name and archive.print_name and persisted_name == archive.print_name:
                        await usage_tracker.clear_persisted_session(db, archive.printer_id)
            except Exception:
                logger.exception("reconcile: usage booking failed for archive %s", archive.id)

        extra = dict(archive.extra_data or {})
        extra["recovered_by_startup_sweep"] = True
        if uncertain:
            extra["recovered_outcome_uncertain"] = True
        archive.extra_data = extra
        swap_owed = status == "completed" and "swap_mode_change_table" in (extra.get("swap_macro_events_pending") or [])

        if status == "completed":
            await _bump_library_file_usage(db, archive.library_file_id)

        item = (
            await db.execute(select(PrintQueueItem).where(PrintQueueItem.archive_id == archive.id))
        ).scalar_one_or_none()
        if item is not None:
            item.status = status
            item.completed_at = now
            if status == "failed":
                await set_queue_error(db, item.queue_id, failed_item_id=item.id)
            elif status == "cancelled":
                await set_queue_paused(db, item.queue_id, paused_item_id=item.id)
            elif not swap_owed:
                await set_queue_idle(db, item.queue_id)
            await update_queue_counters(db, item.queue_id)
            from backend.app.services.plate_hold import clean_up_finished_row

            await clean_up_finished_row(db, item, queue_status=status, plate_auto_cleared=False)

        if archive.printer_id is not None and not swap_owed:
            await printer_manager.arm_awaiting_plate_clear(archive.printer_id, archive.id)

        logger.info(
            "reconcile: closed archive %s as %s%s",
            archive.id,
            status,
            " (outcome uncertain)" if uncertain else "",
        )
        return archive.id
    finally:
        if analysis_leased:
            discard_print_file_analysis(printer_manager, archive.printer_id, archive.id)


def _accepted_terminal_checkpoint(archive: PrintArchive) -> dict | None:
    """Return a v2 terminal checkpoint only when it is safe to interpret."""
    from backend.app.main import _TERMINAL_ACCEPTANCE_KEY

    extra = archive.extra_data if isinstance(archive.extra_data, dict) else {}
    marker = extra.get(_TERMINAL_ACCEPTANCE_KEY)
    if not isinstance(marker, dict) or marker.get("version") != 2 or marker.get("stage") != "accepted":
        return None
    if marker.get("outcome") not in {"completed", "failed", "cancelled"}:
        return None
    if not isinstance(marker.get("queue_item_id"), int):
        return None
    if not isinstance(marker.get("queue_started_at"), str):
        return None
    return marker


def _checkpoint_started_at_matches(marker_value: str, current: datetime) -> bool:
    """Compare the attempt token across SQLite's timezone-less round trip."""
    try:
        recorded = datetime.fromisoformat(marker_value)
    except ValueError:
        return False
    recorded = recorded if recorded.tzinfo else recorded.replace(tzinfo=timezone.utc)
    current = current if current.tzinfo else current.replace(tzinfo=timezone.utc)
    return recorded == current


async def _recover_accepted_terminal_checkpoint(db: AsyncSession, archive: PrintArchive) -> int | None:
    """Close an accepted-but-interrupted terminal without replaying effects.

    A crash after acceptance has no proof whether a macro, physical cleanup,
    notification or accounting operation happened.  The safe recovery is only
    structural: close exactly the archived attempt, pause its queue and leave
    an explicit uncertainty record.  It deliberately does not call the normal
    completion handler or ``_reconcile_complete_archive``.
    """
    from backend.app.main import _TERMINAL_ACCEPTANCE_KEY

    marker = _accepted_terminal_checkpoint(archive)
    if marker is None:
        return None

    item = await db.scalar(
        select(PrintQueueItem)
        .where(
            PrintQueueItem.id == marker["queue_item_id"],
            PrintQueueItem.archive_id == archive.id,
            PrintQueueItem.status == "printing",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        item is None
        or item.started_at is None
        or not _checkpoint_started_at_matches(marker["queue_started_at"], item.started_at)
    ):
        logger.warning(
            "reconcile: accepted terminal checkpoint for archive %s no longer owns its queue attempt; preserving state",
            archive.id,
        )
        return None

    from backend.app.services.queue_counters import set_queue_paused, update_queue_counters

    now = datetime.now(timezone.utc)
    archive.status = marker["outcome"]
    archive.completed_at = _recovered_completed_at(archive.started_at, archive.print_time_seconds, now)
    extra = dict(archive.extra_data or {})
    checkpoint = dict(marker)
    checkpoint["stage"] = "recovered_uncertain"
    extra[_TERMINAL_ACCEPTANCE_KEY] = checkpoint
    extra["terminal_effects_uncertain"] = True
    archive.extra_data = extra
    item.status = marker["outcome"]
    item.completed_at = now
    await set_queue_paused(db, item.queue_id, paused_item_id=item.id, expected_item_id=item.id)
    await update_queue_counters(db, item.queue_id)
    logger.warning(
        "reconcile: structurally closed accepted terminal archive %s as %s; external effects were not replayed",
        archive.id,
        marker["outcome"],
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

    exact_subtask_matches = [archive for archive in orphans if _name_matches_subtask_exact(archive, live_subtask_name)]
    legacy_subtask_matches = (
        []
        if exact_subtask_matches
        else [archive for archive in orphans if _name_matches_subtask_legacy(archive, live_subtask_name)]
    )
    if len(exact_subtask_matches) > 1 or len(legacy_subtask_matches) > 1:
        logger.warning(
            "reconcile: subtask %r matches several printing archives on printer %d; leaving those claims untouched",
            live_subtask_name,
            printer_id,
        )
        return []

    closed = 0
    recovered: list[int] = []
    for archive in orphans:
        # An accepted terminal is stronger evidence than a filename, but it is
        # not permission to replay an unknown physical effect after restart.
        # A live active printer still wins: the terminal may have been stale A
        # while B is already running, so preserve both rows for operator-safe
        # reconciliation instead of closing by a stale checkpoint.
        checkpoint = _accepted_terminal_checkpoint(archive)
        if checkpoint is not None:
            if (live_state or "").upper() not in {"IDLE", "FINISH", "FAILED"}:
                logger.warning(
                    "reconcile: accepted terminal archive %s has active printer state %s; preserving it",
                    archive.id,
                    live_state,
                )
                continue
            recovered_archive = await _recover_accepted_terminal_checkpoint(db, archive)
            if recovered_archive is not None:
                recovered.append(recovered_archive)
                closed += 1
            continue

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
        subtask_match = (
            archive in exact_subtask_matches
            if len(exact_subtask_matches) == 1
            else archive in legacy_subtask_matches
            if len(legacy_subtask_matches) == 1
            else False
        )
        file_match = _file_matches(archive.filename or "", live_file) or subtask_match
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
            recovered.append(await _reconcile_complete_archive(db, archive, status="cancelled", uncertain=True))
        else:  # completed
            recovered.append(await _reconcile_complete_archive(db, archive, status="completed", uncertain=False))
        closed += 1

    if closed:
        logger.info("reconcile: closed %d orphan print(s) on startup for printer %d", closed, printer_id)
    return recovered


class _RepairSnapshotChanged(Exception):
    """Abort the short repair transaction when telemetry/dispatch moves on."""


def _inactive_repair_snapshot(printer_id: int) -> tuple | None:
    from backend.app.services.background_dispatch import background_dispatch
    from backend.app.services.print_scheduler import scheduler
    from backend.app.services.printer_manager import printer_manager

    state, received_at, stale = printer_manager.peek_status(printer_id)
    if (
        state is None
        or not state.connected
        or received_at is None
        or stale
        or _live_completions.get(printer_id, 0)
        or state.state not in {"IDLE", "FINISH", "FAILED"}
        or scheduler.has_dispatch_in_flight(printer_id)
        or background_dispatch.has_work_for_printer(printer_id)
    ):
        return None
    return (id(state), state.state, state.connection_generation, state.subtask_id, state.subtask_name, state.gcode_file)


async def _repair_terminal_queue_items(
    db: AsyncSession, printer_id: int, live_state: str
) -> list[tuple[int, int, str]]:
    """Structural repair only; caller owns a dedicated transaction and its commit.

    A terminal archive is not an instruction to replay completion. Never call
    accounting, macros, gate/receipt handlers, notifications or row deletion.
    Leave a repaired queue paused for inspection, without replacing its gate.
    All printing rows must be unambiguous; an active/unknown neighbour defers
    the whole queue. No name matching or guessed terminal result is involved.
    """
    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.services.queue_counters import update_queue_counters
    from backend.app.services.queue_ops import queue_scope_lock

    snapshot = _inactive_repair_snapshot(printer_id)
    if snapshot is None or snapshot[1] != live_state:
        return []
    queue_id = await db.scalar(select(PrinterQueue.id).where(PrinterQueue.printer_id == printer_id))
    if queue_id is None:
        return []

    async with queue_scope_lock(db, queue_id):
        # Obtain SQLite's writer (and PG's queue row lock) BEFORE reading the
        # authoritative item/archive/header snapshot. No physical I/O here.
        await db.execute(
            update(PrinterQueue)
            .where(PrinterQueue.id == queue_id)
            .values(last_activity_at=PrinterQueue.last_activity_at, updated_at=PrinterQueue.updated_at)
        )
        queue = await db.get(PrinterQueue, queue_id, populate_existing=True)
        items = list(
            (
                await db.scalars(
                    select(PrintQueueItem)
                    .where(PrintQueueItem.queue_id == queue_id, PrintQueueItem.status == "printing")
                    .order_by(PrintQueueItem.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        if not items:
            return []

        def defer(reason: str) -> list:
            logger.warning(
                "reconcile: terminal queue repair deferred printer=%s queue=%s reason=%s", printer_id, queue_id, reason
            )
            return []

        if queue.current_item_id is not None and queue.current_item_id not in {item.id for item in items}:
            return defer("different_current_item")
        if await db.scalar(
            select(PrintArchive.id)
            .where(PrintArchive.printer_id == printer_id, PrintArchive.status == "printing")
            .limit(1)
        ):
            return defer("printing_archive")

        outcomes = {
            "completed": "completed",
            "failed": "failed",
            "cancelled": "cancelled",
            "aborted": "cancelled",
            "stopped": "cancelled",
        }
        pairs = []
        seen = set()
        now = datetime.now(timezone.utc)
        for item in items:
            archive = await db.scalar(
                select(PrintArchive)
                .where(PrintArchive.id == item.archive_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if archive is None or archive.id in seen:
                return defer("missing_or_shared_archive")
            seen.add(archive.id)
            if archive.printer_id != printer_id or archive.queue_id != queue_id or archive.deleted_at is not None:
                return defer("archive_ownership")
            provenance = (item.source_snapshot or {}).get("provenance") or {}
            if provenance.get("kind") == "archive" and provenance.get("id") == archive.id:
                return defer("source_archive")
            if archive.status not in outcomes or (archive.extra_data or {}).get("recovered_outcome_uncertain"):
                return defer("unproven_outcome")
            # archive_id can still name the SOURCE of a repeat whose dispatcher
            # never created its execution archive. That old finish predates the
            # new claim and must NOT be copied onto the new attempt.
            if item.started_at is None or archive.completed_at is None:
                return defer("missing_run_times")
            claimed = item.started_at.replace(tzinfo=timezone.utc)
            ended = archive.completed_at.replace(tzinfo=timezone.utc)
            if ended <= claimed or ended > now:
                return defer("source_archive_or_invalid_times")
            if archive.started_at and archive.started_at.replace(tzinfo=timezone.utc) > ended:
                return defer("invalid_archive_times")
            pairs.append((item, archive, outcomes[archive.status]))

        if _inactive_repair_snapshot(printer_id) != snapshot:
            raise _RepairSnapshotChanged
        for item, archive, outcome in pairs:
            item.status = outcome
            item.completed_at = archive.completed_at
            item.waiting_reason = None
            if outcome != "completed" and not item.error_message:
                item.error_message = archive.failure_reason
        queue.current_item_id = None
        if queue.status != "error":
            queue.status = "paused"
        queue.is_paused = True
        queue.last_activity_at = now
        await update_queue_counters(db, queue_id)
        await db.flush()
        if _inactive_repair_snapshot(printer_id) != snapshot:
            raise _RepairSnapshotChanged
        return [(item.id, archive.id, outcome) for item, archive, outcome in pairs]


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
        # Separate transaction: already-terminal archives must never enter the
        # recovery list below (energy, usage and swaps would be replayed).
        async with async_session() as db:
            repaired = await _repair_terminal_queue_items(db, printer_id, live_state)
            await db.commit()
        for item_id, archive_id, outcome in repaired:
            logger.info(
                "reconcile: repaired terminal queue item printer=%s item=%s archive=%s outcome=%s; queue paused for inspection",
                printer_id,
                item_id,
                archive_id,
                outcome,
            )
        async with async_session() as db:
            recovered = await _reconcile(db, printer_id, live_state, live_file, live_subtask_id, live_subtask_name)
            await db.commit()
    except _RepairSnapshotChanged:
        logger.info("reconcile: terminal queue repair deferred printer=%s reason=live_snapshot_changed", printer_id)
        return
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

    # The table swap a dead process still owed (2026-08-29). Spawned after the
    # commit: the macro is a physical move with an ACK wait, which has no
    # business on the connect path — and the race is already closed, because
    # the close above kept the queue claim for every archive whose swap is
    # owed, so nothing dispatches until this task settles it.
    if recovered:
        from backend.app.core.tasks import spawn_background_task

        spawn_background_task(
            _resolve_pending_swaps(printer_id, recovered),
            name=f"reconcile-swaps-{printer_id}",
        )

    await _load_objects_for_a_print_already_running(printer_id, live_state)


async def _resolve_pending_swaps(printer_id: int, archive_ids: list[int]) -> None:
    """Finish the table swap a dead process still owed, or hold the queue.

    For each recovered archive still carrying ``swap_mode_change_table`` on
    its pending checklist:

    * **certain completed + swap enabled + macro found** — run the macro now,
      exactly as the live completion would have, then release the queue claim
      the close deliberately kept. The interrupted automation resumes.
    * **anything else** — outcome uncertain, swap since disabled, macro
      missing, or the macro failed — pause the queue with a reason on the
      next pending item. ``waiting_reason`` alone is NOT a block (the
      scheduler recomputes it every tick); the pause is.
    """
    from sqlalchemy import select as sa_select

    from backend.app.core.database import async_session
    from backend.app.models.archive import PrintArchive
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.printer import Printer
    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.services.archive import remove_swap_pending_event
    from backend.app.services.macro_executor import find_swap_macro
    from backend.app.services.printer_manager import printer_manager
    from backend.app.services.queue_counters import set_queue_idle, set_queue_paused

    for archive_id in archive_ids:
        try:
            async with async_session() as db:
                archive = await db.get(PrintArchive, archive_id)
                if archive is None or archive.status != "completed":
                    continue
                extra = archive.extra_data if isinstance(archive.extra_data, dict) else {}
                if "swap_mode_change_table" not in (extra.get("swap_macro_events_pending") or []):
                    continue
                uncertain = bool(extra.get("recovered_outcome_uncertain"))
                printer = await db.get(Printer, printer_id)
                # ⚠️ queue_id is NOT printer_id — see telegram_handlers.common.resolve_queue_id.
                queue_id = (
                    await db.execute(sa_select(PrinterQueue.id).where(PrinterQueue.printer_id == printer_id))
                ).scalar_one_or_none()

                macro = None
                if printer is not None and printer.swap_mode_enabled and not uncertain:
                    macro = await find_swap_macro(db, "swap_mode_change_table", printer)

                swapped = False
                reason = None
                if uncertain:
                    reason = "Swap pending after outage — outcome uncertain, inspect the plate, then resume"
                elif macro is None or not macro.gcode:
                    reason = "Swap pending after outage — run the table swap or clear the plate, then resume"
                else:
                    logger.info(
                        "reconcile: running owed change_table macro '%s' on printer %d (archive %d)",
                        macro.name,
                        printer_id,
                        archive_id,
                    )
                    swapped, msg = await printer_manager.execute_macro_and_wait(printer_id, macro.gcode, macro.name)
                    if swapped:
                        if remove_swap_pending_event(archive, "swap_mode_change_table"):
                            await db.commit()
                    else:
                        reason = f"Swap macro failed: {msg}"

                if swapped:
                    if queue_id is not None:
                        await set_queue_idle(db, queue_id)
                        await db.commit()
                    logger.info("reconcile: owed table swap done — queue released for printer %d", printer_id)
                elif queue_id is not None:
                    await set_queue_paused(db, queue_id)
                    nxt = (
                        await db.execute(
                            sa_select(PrintQueueItem)
                            .where(PrintQueueItem.queue_id == queue_id, PrintQueueItem.status == "pending")
                            .order_by(PrintQueueItem.position)
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if nxt is not None:
                        nxt.waiting_reason = reason
                    await db.commit()
                    logger.warning("reconcile: queue paused for printer %d — %s", printer_id, reason)
        except Exception:
            logger.exception("reconcile: swap resolution failed for archive %s", archive_id)


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
    released only when all four hold:

    1. the queue names the item holding the claim. An external or direct print
       claims with ``current_item_id=None``; its truth lives in MQTT, not in our
       tables, and we have nothing to prove here — left alone.
    2. that item is still ``printing``.
    3. The item has no archive link (including a terminal/source/missing archive).
       A linked archive is not proof of an interrupted, unpublished dispatch.
    4. **the printer has no archive in ``printing``.** This is the load-bearing
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

        # A linked archive may already be terminal while the queue update was
        # lost. Returning that item to pending would print it a second time.
        # Even a missing/source archive is ambiguous here: wait for fresh MQTT
        # reconciliation rather than declaring that nothing was ever sent.
        if item.archive_id is not None:
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
