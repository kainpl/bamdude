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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings
from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services import dispatch_phase
from backend.app.utils.failure_reasons import NO_STATUS_UPDATE, row_class
from backend.app.utils.filename import derive_remote_filename
from backend.app.utils.safe_path import PathTraversalError, safe_join_under

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _OwnClose:
    """A run THIS sweep took from printing to completed/failed itself -- never an uncertain or checkpoint close.

    Process-local and per call: the one thing that lets the wrapper release that run's context after its outer
    commit (spec gh109-o3 r2 R1/R2). Returned archive ids are no such proof.
    """

    archive_id: int
    queue_item_id: int | None
    item_started_at: datetime | None
    subtask_id: str | None
    swap_owed: bool


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


def live_completion_active(printer_id: int) -> bool:
    """Read-only: a live completion of this printer is between its archive and queue writes."""
    return _live_completions.get(printer_id, 0) > 0


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
    report: list[_OwnClose] | None = None,
) -> int | None:
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

    Returns the archive id, or ``None`` when the archive is no longer printing -- another sweep closed it while
    this one held a stale copy (two connect edges at once, Codex review r1): nothing is closed, booked or swapped a
    second time. The caller commits.
    """
    from backend.app.core.database import take_write_lock
    from backend.app.main import _TERMINAL_ACCEPTANCE_KEY, _bump_library_file_usage
    from backend.app.services import swap_terminal
    from backend.app.services.archive_write_scope import locked_print
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

    # SQLite's writer (PostgreSQL's row lock) BEFORE the authoritative read: a second sweep waits here and then sees
    # what the first one committed.
    await take_write_lock(db, PrintArchive.__table__, archive.id)
    still = await locked_print(
        db, archive.id, select(PrintArchive.status).where(PrintArchive.id == archive.id).with_for_update()
    )
    if still != "printing":
        logger.info(
            "reconcile: archive %s is %s already -- another sweep closed it; nothing to close", archive.id, still
        )
        return None

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
        # A key, not a sentence (upstream #2974): both cases observed the same
        # thing — no end-of-print status ever arrived — and ``status`` already
        # says whether it was reconciled as failed or as an uncertain cancel.
        if (
            status == "failed"
            and not archive.failure_reason
            or status == "cancelled"
            and uncertain
            and not archive.failure_reason
        ):
            archive.failure_reason = NO_STATUS_UPDATE

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

        # The metadata as it is NOW, under the print's lock: an attach may have
        # finished the swap declaration during the awaits above, and the copy
        # this function was handed predates them (swap-source-lifecycle spec r4
        # §7). Only this sweep's own keys are merged in.
        current = await locked_print(
            db,
            archive.id,
            select(PrintArchive.extra_data).where(PrintArchive.id == archive.id).with_for_update(),
        )
        extra = dict(current) if isinstance(current, dict) else {}
        extra["recovered_by_startup_sweep"] = True
        if uncertain:
            extra["recovered_outcome_uncertain"] = True
        swap_owed = status == "completed" and "swap_mode_change_table" in (extra.get("swap_macro_events_pending") or [])
        if swap_owed and _TERMINAL_ACCEPTANCE_KEY not in extra:
            # Still printing, no finish ever accepted: nothing can have attempted our change_table -- the proof a
            # later sweep needs before it may send it (Codex review r1).
            extra = swap_terminal.mark_owed(extra)
        archive.extra_data = extra

        if status == "completed":
            await _bump_library_file_usage(db, archive.library_file_id)

        # Persist the archive and arm the existing gate BEFORE releasing the
        # queue claim, just as the live completion handler does.
        await db.commit()
        if archive.printer_id is not None and not swap_owed:
            await printer_manager.arm_awaiting_plate_clear(archive.printer_id, archive.id)

        # The row that RAN this archive -- still holding the printer. A job queued from the archive later (a
        # reprint names it as its source) is not this run and is never closed by it.
        item = (
            await db.execute(
                select(PrintQueueItem).where(
                    PrintQueueItem.archive_id == archive.id, PrintQueueItem.status == "printing"
                )
            )
        ).scalar_one_or_none()
        if item is not None:
            item.status = status
            item.completed_at = now
            if status == "failed":
                item.error_code = row_class(status, archive.failure_reason)
                await set_queue_error(db, item.queue_id, failed_item_id=item.id)
            elif status == "cancelled":
                await set_queue_paused(db, item.queue_id, paused_item_id=item.id)
            elif not swap_owed:
                await set_queue_idle(db, item.queue_id)
            await update_queue_counters(db, item.queue_id)
            # ⚠️ Not while our table swap is owed: this row is the proof of the run ``_resolve_pending_swaps``
            # decides on, and it cleans the row up itself after a swap (swap-source-lifecycle spec r4 R2-5).
            if not swap_owed:
                from backend.app.services.plate_hold import clean_up_finished_row

                await clean_up_finished_row(db, item, queue_status=status, plate_auto_cleared=False)

        logger.info(
            "reconcile: closed archive %s as %s%s",
            archive.id,
            status,
            " (outcome uncertain)" if uncertain else "",
        )
        if report is not None and status in ("completed", "failed") and not uncertain:
            report.append(
                _OwnClose(
                    archive_id=archive.id,
                    queue_item_id=item.id if item is not None else None,
                    item_started_at=item.started_at if item is not None else None,
                    subtask_id=archive.subtask_id,
                    swap_owed=swap_owed,
                )
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

    from backend.app.core.database import take_write_lock

    # The writer before the authoritative read: a second sweep holding the same stale copy waits, then finds the
    # attempt no longer printing and closes nothing (Codex review r1).
    await take_write_lock(db, PrintArchive.__table__, archive.id)
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
    # The metadata as it is now, under the print's lock -- the row this sweep was
    # handed may predate an attach that finished its swap declaration
    # (swap-source-lifecycle spec r4 §7).
    from backend.app.services.archive_write_scope import locked_print

    current = await locked_print(
        db,
        archive.id,
        select(PrintArchive.extra_data).where(PrintArchive.id == archive.id).with_for_update(),
    )
    extra = dict(current) if isinstance(current, dict) else {}
    checkpoint = dict(marker)
    checkpoint["stage"] = "recovered_uncertain"
    extra[_TERMINAL_ACCEPTANCE_KEY] = checkpoint
    extra["terminal_effects_uncertain"] = True
    archive.extra_data = extra
    item.status = marker["outcome"]
    item.completed_at = now
    item.error_code = row_class(item.status, archive.failure_reason)
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
    *,
    record_lives=None,
    own_closes: list[_OwnClose] | None = None,
) -> list[int]:
    """Reconcile every orphan ``printing`` archive for one printer.

    Returns the ids of the archives it closed, for the caller's post-commit
    energy recovery. Takes an explicit session so tests can drive it directly;
    :func:`reconcile_printer_prints` is the production wrapper. ``record_lives``
    is asked before each archive is closed: a deletion of the printer record
    meanwhile ends the sweep (spec gh109-o1 r2 R6).
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
        if record_lives is not None and not record_lives():
            break
        if archive.dispatch_phase in dispatch_phase.UNACCEPTED and _accepted_terminal_checkpoint(archive) is None:
            # I-R1: our dispatch never saw this attempt running. What the printer shows —
            # often the PREVIOUS print of the same plate, FINISH under the same name —
            # proves nothing about it, so no filename verdict closes it and nothing of a
            # completion runs. Its own runner ends it; an ended owner's attempt is for the
            # interrupted-dispatch recovery and the Info action (``interrupted_dispatch``).
            # A finish our completion handler already accepted for it is proof it ran — that
            # one goes on to the checkpoint below.
            logger.info(
                "reconcile: archive %d on printer %d is a dispatch attempt in phase %s — not closed by the "
                "printer's state",
                archive.id,
                printer_id,
                archive.dispatch_phase,
            )
            continue
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
            closed_id = await _reconcile_complete_archive(
                db, archive, status="failed", uncertain=False, report=own_closes
            )
        elif action == "uncertain":
            closed_id = await _reconcile_complete_archive(db, archive, status="cancelled", uncertain=True)
        else:  # completed
            closed_id = await _reconcile_complete_archive(
                db, archive, status="completed", uncertain=False, report=own_closes
            )
        if closed_id is not None:
            recovered.append(closed_id)
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
    db: AsyncSession, printer_id: int, live_state: str, *, candidates: tuple | None = None
) -> list[tuple[int, int, str]]:
    """Structural repair only; caller owns a dedicated transaction and its commit.

    A terminal archive is not an instruction to replay completion. Never call
    accounting, macros, gate/receipt handlers, notifications or row deletion.
    Leave a repaired queue paused for inspection, without replacing its gate.
    All printing rows must be unambiguous; an active/unknown neighbour defers
    the whole queue. No name matching or guessed terminal result is involved.
    """
    from contextlib import AsyncExitStack

    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.services.archive_write_scope import load_active_archive_for_write
    from backend.app.services.queue_counters import update_queue_counters
    from backend.app.services.queue_ops import claim_lanes

    snapshot = _inactive_repair_snapshot(printer_id)
    if snapshot is None or snapshot[1] != live_state:
        return []
    queue_id = await db.scalar(select(PrinterQueue.id).where(PrinterQueue.printer_id == printer_id))
    if queue_id is None:
        return []

    if candidates is None:
        candidates = tuple(
            (
                await db.execute(
                    select(PrintQueueItem.id, PrintQueueItem.archive_id)
                    .where(PrintQueueItem.queue_id == queue_id, PrintQueueItem.status == "printing")
                    .order_by(PrintQueueItem.id)
                )
            ).all()
        )
    async with AsyncExitStack() as guards:
        archive_ids = sorted({aid for _, aid in candidates if aid is not None})
        # Acquire ALL archive/lane process locks before SQLite's writer, then DB guards in that same order.
        await guards.enter_async_context(claim_lanes(db, [queue_id], archive_ids=archive_ids))
        for archive_id in archive_ids:
            if db.get_bind().dialect.name == "sqlite":
                await db.execute(update(PrintArchive).where(PrintArchive.id == archive_id).values(id=PrintArchive.id))
            await load_active_archive_for_write(db, archive_id)
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
        if tuple((item.id, item.archive_id) for item in items) != candidates:
            return []  # A new owner appeared: defer rather than reverse the lock order.

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
            item.error_code = row_class(outcome, archive.failure_reason)
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


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class _OwnRunCapture:
    """The run bound here when the sweep began, and the fences it must still hold when the sweep released it.

    The sweep that itself closes that exact run as completed or failed ends its context the way a live
    completion would (owner decision D2 = C, spec gh109-o3 r2): once, after its outer commit, by comparing with
    THIS capture -- never by re-reading a newer run -- and with no new pause, intent or audit. Anything less --
    an uncertain or checkpoint close, a swap still owed, a changed context, another client session, record life
    or storage epoch -- leaves the context to the operator's audited repair.
    """

    printer_id: int
    expected: object | None
    client_generation: int | None
    printer_life: int | None
    storage_epoch: int

    @classmethod
    def take(cls, printer_id: int, printer_life: int | None) -> _OwnRunCapture:
        from backend.app.services.print_run_binding import capture_runtime_context
        from backend.app.services.printer_manager import printer_manager
        from backend.app.services.queue_sources import current_epoch

        return cls(
            printer_id=printer_id,
            expected=capture_runtime_context(printer_manager, printer_id),
            client_generation=printer_manager.current_client_generation(printer_id),
            printer_life=printer_life,
            storage_epoch=current_epoch(),
        )

    def _closed_this_run(self, own_closes: list[_OwnClose]) -> bool:
        run = self.expected.current
        closes = [close for close in own_closes if close.archive_id == run.archive_id]
        if len(closes) != 1 or closes[0].swap_owed:
            return False
        close = closes[0]
        # A numeric archive id is no proof on its own (ids are reused): the claim or the device id must match too.
        if run.queue_item_id is not None:
            if close.queue_item_id != run.queue_item_id:
                return False
            return run.claim_started_at is None or _as_utc(close.item_started_at) == _as_utc(run.claim_started_at)
        return bool(run.observed_subtask_id) and str(close.subtask_id or "") == run.observed_subtask_id

    def release_if_closed(self, own_closes: list[_OwnClose]) -> bool:
        from backend.app.services.print_run_binding import release_runtime_context, runtime_context_unchanged
        from backend.app.services.printer_manager import printer_manager
        from backend.app.services.queue_sources import current_epoch

        if self.expected is None or not self._closed_this_run(own_closes):
            return False
        pid = self.printer_id
        if (
            printer_manager.current_client_generation(pid) != self.client_generation
            or not printer_manager.life_holds(pid, self.printer_life)
            or current_epoch() != self.storage_epoch
            or not runtime_context_unchanged(printer_manager, self.expected)
        ):
            logger.info(
                "reconcile: printer %d archive %d closed, its context moved meanwhile -- kept for the operator",
                pid,
                self.expected.current.archive_id,
            )
            return False
        release_runtime_context(printer_manager, self.expected)
        logger.info(
            "reconcile: printer %d archive %d closed by this sweep -- its run context is released",
            pid,
            self.expected.current.archive_id,
        )
        return True


async def reconcile_printer_prints(
    printer_id: int,
    live_state: str,
    live_file: str,
    live_subtask_id: str = "",
    live_subtask_name: str = "",
    *,
    printer_life: int | None = None,
) -> None:
    """Entry point — runs on the first full MQTT status after each fresh
    connect. Opens its own session and commits.

    ``printer_life`` is the record life of the session that reported the
    status: once that record is deleted, the next printer created with its id
    is never swept with the old one's live state (spec gh109-o1 r2 R6). Proved
    at the start and again before every stage that writes."""
    from backend.app.core.database import async_session
    from backend.app.services.print_usage_journal import journal_lock, settle_pending_replay
    from backend.app.services.printer_manager import printer_manager

    def record_lives() -> bool:
        if printer_manager.life_holds(printer_id, printer_life):
            return True
        logger.info("reconcile: printer %d was deleted since this status -- sweep abandoned", printer_id)
        return False

    if not record_lives():
        return
    # What a close of the run still bound here may release, captured before anything awaits (spec gh109-o3 r2 R2).
    own = _OwnRunCapture.take(printer_id, printer_life)
    own_closes: list[_OwnClose] = []
    recovered: list[int] = []
    try:
        # A restart replay still waiting is written first, while its archive is still the
        # printing one — before this sweep may close that archive and book its usage, under a
        # writer it then holds (post-rebuild F3) — or, when it cannot be, stated unknown (F4).
        async with journal_lock(printer_id):
            await settle_pending_replay(printer_id)
        # Separate transaction: already-terminal archives must never enter the
        # recovery list below (energy, usage and swaps would be replayed).
        async with async_session() as preflight:
            from backend.app.models.printer_queue import PrinterQueue

            queue_id = await preflight.scalar(select(PrinterQueue.id).where(PrinterQueue.printer_id == printer_id))
            candidates = tuple(
                (
                    await preflight.execute(
                        select(PrintQueueItem.id, PrintQueueItem.archive_id)
                        .where(PrintQueueItem.queue_id == queue_id, PrintQueueItem.status == "printing")
                        .order_by(PrintQueueItem.id)
                    )
                ).all()
            )
        if not record_lives():
            return
        async with async_session() as db:
            repaired = await _repair_terminal_queue_items(db, printer_id, live_state, candidates=candidates)
            if not record_lives():
                await db.rollback()
                return
            await db.commit()
        for item_id, archive_id, outcome in repaired:
            logger.info(
                "reconcile: repaired terminal queue item printer=%s item=%s archive=%s outcome=%s; queue paused for inspection",
                printer_id,
                item_id,
                archive_id,
                outcome,
            )
        if not record_lives():
            return
        async with async_session() as db:
            recovered = await _reconcile(
                db,
                printer_id,
                live_state,
                live_file,
                live_subtask_id,
                live_subtask_name,
                record_lives=record_lives,
                own_closes=own_closes,
            )
            await db.commit()
        # The outer commit is confirmed and nothing has awaited since: one compare-and-clear, or nothing.
        own.release_if_closed(own_closes)
    except _RepairSnapshotChanged:
        logger.info("reconcile: terminal queue repair deferred printer=%s reason=live_snapshot_changed", printer_id)
        return
    except Exception:  # noqa: BLE001 — a background sweep must never crash the connect path
        logger.exception("reconcile: connect-edge sweep failed for printer %d", printer_id)

    if not record_lives():
        return

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
    #
    # A sweep that closed a run and never settled its swap (the process stopped, the resolver raised) left the
    # printer's queue holding that completed run (Codex review r1, R3). An ``owed`` claim proves nothing was sent:
    # settled once, in the background with the rest; anything else is bookkeeping only, done here.
    try:
        resume, settle = await _stranded_swaps(printer_id, exclude=set(recovered))
    except Exception:  # noqa: BLE001 — the connect path survives it; the next edge looks again
        logger.exception("reconcile: could not look for an unsettled table swap on printer %d", printer_id)
        resume, settle = [], []
    if settle:
        await _resolve_pending_swaps(printer_id, settle)
    swaps = recovered + resume
    if swaps:
        from backend.app.core.tasks import spawn_background_task

        spawn_background_task(
            _resolve_pending_swaps(printer_id, swaps),
            name=f"reconcile-swaps-{printer_id}",
        )

    await _load_objects_for_a_print_already_running(printer_id, live_state)

    # The addressed recheck of an ended owner's interrupted dispatch (post-rebuild I-R3): this
    # printer now reports a fresh state, the one thing the startup classification lacked.
    try:
        from backend.app.services.interrupted_dispatch import recover_interrupted_dispatches

        await recover_interrupted_dispatches(printer_id)
    except Exception:  # noqa: BLE001 — the connect path survives it; Info shows the attempt
        logger.exception("reconcile: interrupted-dispatch recheck failed for printer %d", printer_id)

    # Finished rows a later print has superseded -- a held swap's record and its router row -- can never
    # settle any more: they are the ordinary cleanup that was skipped (queue-leftovers spec Q3). Never raises.
    from backend.app.services.queue_leftovers import retire_for_printer

    await retire_for_printer(printer_id)


async def _stranded_swaps(printer_id: int, *, exclude: set[int]) -> tuple[list[int], list[int]]:
    """The completed run that still holds this printer's queue with our change_table unsettled.

    ``(resume, settle)``: an archive whose claim is ``owed`` (its command may be sent once, by the resolver in the
    background); one whose claim is anything else nobody alive is sending -- attempted, never recorded, or answered
    with its bookkeeping never run -- settled without a command. Never while a live completion is in flight here.
    """
    from backend.app.core.database import async_session
    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.services import swap_terminal

    if _live_completions.get(printer_id, 0):
        return [], []
    async with async_session() as db:
        queue = await db.scalar(select(PrinterQueue).where(PrinterQueue.printer_id == printer_id))
        if queue is None or queue.status != "printing" or queue.current_item_id is None:
            return [], []
        run = await db.get(PrintQueueItem, queue.current_item_id)
        if run is None or run.status != "completed" or run.archive_id is None or run.archive_id in exclude:
            return [], []
        archive = await db.get(PrintArchive, run.archive_id)
        if archive is None or archive.status != "completed" or archive.printer_id != printer_id:
            return [], []
        extra = archive.extra_data if isinstance(archive.extra_data, dict) else {}
        stage = swap_terminal.claim_stage(extra)
        pending = swap_terminal.CHANGE_TABLE_EVENT in (extra.get("swap_macro_events_pending") or [])
        if swap_terminal.sending(archive.id) or not (pending or stage in swap_terminal.ANSWERED):
            return [], []
        logger.info(
            "reconcile: printer %d still holds completed run %d with its table swap unsettled (claim %s)",
            printer_id,
            run.id,
            stage,
        )
        if pending and stage == swap_terminal.CLAIM_OWED:
            return [archive.id], []
        return [], [archive.id]


async def _resolve_pending_swaps(printer_id: int, archive_ids: list[int]) -> None:
    """Finish the table swap a dead process still owed, or hold the queue.

    For each recovered archive still carrying ``swap_mode_change_table`` on its pending checklist the decision is
    the live completion's (swap-source-lifecycle spec r4 §10 D3, ``services/swap_terminal``), and the command goes
    out at most once, only for the run that still holds its printer (Codex review r1):

    * **a proven run over a source known not to carry its own sequence** -- the outcome certain, the claim ``owed``
      (nothing attempted it), the run still the queue's claim, no other run active, the printer in swap mode with its
      macro, the declaration False -- the claim is compare-and-set to ``attempted`` and committed, the macro runs
      under a fence, its answer is recorded with the event, and the claim the run kept is released;
    * **a claim another owner in this process is sending** -- nothing: that owner settles it;
    * **anything else** -- True, unknown, an uncertain outcome, a claim attempted by nobody alive or never recorded,
      no proof of the run, swap mode off, no macro, a failed macro, an error -- nothing is sent (again), the hold is
      recorded on the archive, and the queue is paused with the reason on the run's row and the next job, only
      while this run still holds the queue: a run that lost it writes its own history and leaves the new owner
      alone. ``waiting_reason`` alone is NOT a block (the scheduler recomputes it every tick); the pause is.

    A claim answered whose bookkeeping never ran (the process stopped after the answer) is settled the same way:
    released after a finished swap, held after a failed one.
    """
    from types import SimpleNamespace

    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer
    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.services import swap_terminal
    from backend.app.services.macro_executor import find_swap_macro
    from backend.app.services.printer_manager import printer_manager

    def no_dispatch_here() -> bool:
        """The fence while the macro waits: no new dispatch has taken this printer (in memory, no I/O)."""
        from backend.app.services.background_dispatch import background_dispatch
        from backend.app.services.print_scheduler import scheduler

        return not (
            scheduler.has_dispatch_in_flight(printer_id) or background_dispatch.has_work_for_printer(printer_id)
        )

    async def resolve(archive_id: int, state) -> None:
        async with async_session() as db:
            archive = await db.get(PrintArchive, archive_id)
            if archive is None or archive.status != "completed":
                return
            extra = archive.extra_data if isinstance(archive.extra_data, dict) else {}
            pending = swap_terminal.CHANGE_TABLE_EVENT in (extra.get("swap_macro_events_pending") or [])
            stage = swap_terminal.claim_stage(extra)
            state.cycle = swap_terminal.cycle_state(extra)
            if swap_terminal.sending(archive_id):
                logger.info(
                    "reconcile: archive %d's table swap is being sent by another owner -- left to it", archive_id
                )
                return
            if not pending and stage not in swap_terminal.ANSWERED:
                return
            printer = await db.get(Printer, printer_id)
            # ⚠️ queue_id is NOT printer_id — see telegram_handlers.common.resolve_queue_id.
            state.queue_id = (
                await db.execute(select(PrinterQueue.id).where(PrinterQueue.printer_id == printer_id))
            ).scalar_one_or_none()
            state.run = await _swap_run(db, archive_id, state.queue_id)
            run = state.run
            if not pending:
                # Answered, its bookkeeping never ran: release after a swap, hold after a failure.
                swapped = stage == swap_terminal.CLAIM_FINISHED
                reason = None if swapped else swap_terminal.hold_text(swap_terminal.HOLD_MACRO_ERROR, state.cycle)
                await _swap_settle(db, printer_id, archive_id, state.queue_id, run, swapped=swapped, reason=reason)
                return

            if extra.get("recovered_outcome_uncertain") or extra.get("terminal_effects_uncertain"):
                hold = swap_terminal.HOLD_OUTCOME_UNCERTAIN
            elif stage != swap_terminal.CLAIM_OWED:
                hold = swap_terminal.HOLD_ATTEMPT_UNKNOWN
            else:
                execution_owned = run is not None and not await _another_run_active(db, printer_id, archive_id)
                fact = await swap_terminal.pending_fact(db, archive, run)
                hold = swap_terminal.change_table_hold(
                    fact,
                    completed=True,
                    execution_owned=execution_owned,
                    swap_mode=bool(printer is not None and printer.swap_mode_enabled),
                )
            macro = None
            if hold is None:
                macro = await find_swap_macro(db, "swap_mode_change_table", printer)
                if macro is None or not macro.gcode:
                    hold = swap_terminal.HOLD_SWAP_OFF
            if hold is None:

                async def still_ours(session) -> bool:
                    return await _run_holds_its_printer(session, printer_id, archive_id, state.queue_id, run)

                outcome = await swap_terminal.claim_change_table(
                    async_session,
                    archive_id,
                    queue_id=state.queue_id,
                    owner="sweep",
                    allow_absent=False,
                    still_ours=still_ours,
                )
                if outcome == swap_terminal.CLAIM_BUSY:
                    return
                if outcome == swap_terminal.CLAIMED:
                    state.claimed = True
                else:
                    hold = (
                        swap_terminal.HOLD_RUN_UNPROVEN
                        if outcome == swap_terminal.CLAIM_LOST
                        else swap_terminal.HOLD_ATTEMPT_UNKNOWN
                    )

            if state.claimed:
                logger.info(
                    "reconcile: running owed change_table macro '%s' on printer %d (archive %d)",
                    macro.name,
                    printer_id,
                    archive_id,
                )
                swapped, msg = await printer_manager.execute_macro_and_wait(
                    printer_id, macro.gcode, macro.name, may_run=no_dispatch_here
                )
                await swap_terminal.finish_change_table(async_session, archive_id, ok=bool(swapped))
                state.answered = True
                reason = None if swapped else f"Swap macro failed: {msg}"
                ours = await _swap_settle(
                    db, printer_id, archive_id, state.queue_id, run, swapped=bool(swapped), reason=reason
                )
                if swapped:
                    logger.info(
                        "reconcile: owed table swap done%s for printer %d",
                        " — queue released" if ours else " — the queue is no longer this run's",
                        printer_id,
                    )
                else:
                    logger.warning("reconcile: table swap failed on printer %d — %s", printer_id, reason)
                return

            await _swap_record_hold(db, archive_id, hold, state.cycle)
            await _swap_settle(
                db,
                printer_id,
                archive_id,
                state.queue_id,
                run,
                swapped=False,
                reason=swap_terminal.hold_text(hold, state.cycle),
            )
            logger.warning("reconcile: table swap held on printer %d — %s", printer_id, hold)

    for archive_id in archive_ids:
        state = SimpleNamespace(
            claimed=False, answered=False, queue_id=None, run=None, cycle=swap_terminal.CYCLE_POSSIBLY_STARTED
        )
        try:
            await resolve(archive_id, state)
        except Exception:
            logger.exception("reconcile: swap resolution failed for archive %s", archive_id)
            if not state.answered:
                # The run's own claim ends explained, never in silence (Codex review r1, R3): nothing went out
                # before the claim; after it, the command may have.
                hold = swap_terminal.HOLD_ATTEMPT_UNKNOWN if state.claimed else swap_terminal.HOLD_MACRO_ERROR
                try:
                    async with async_session() as db:
                        if state.queue_id is None:
                            state.queue_id = (
                                await db.execute(select(PrinterQueue.id).where(PrinterQueue.printer_id == printer_id))
                            ).scalar_one_or_none()
                            state.run = await _swap_run(db, archive_id, state.queue_id)
                        await _swap_record_hold(db, archive_id, hold, state.cycle)
                        await _swap_settle(
                            db,
                            printer_id,
                            archive_id,
                            state.queue_id,
                            state.run,
                            swapped=False,
                            reason=swap_terminal.hold_text(hold, state.cycle),
                        )
                except Exception:
                    logger.exception("reconcile: could not hold printer %d after the error", printer_id)
        finally:
            if state.claimed:
                swap_terminal.release(archive_id)


async def _swap_run(db, archive_id: int, queue_id: int | None, *, live: bool = False) -> PrintQueueItem | None:
    """The execution-linked job; live completion has not closed its printing row yet."""
    if queue_id is None:
        return None
    rows = (
        (
            await db.execute(
                select(PrintQueueItem).where(
                    PrintQueueItem.archive_id == archive_id,
                    PrintQueueItem.queue_id == queue_id,
                    PrintQueueItem.status == ("printing" if live else "completed"),
                    PrintQueueItem.started_at.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return rows[0] if len(rows) == 1 else None


async def _another_run_active(db, printer_id: int, archive_id: int) -> bool:
    """A print running on the printer now, or another archive of it still printing."""
    from backend.app.services.printer_manager import printer_manager

    live = printer_manager.get_status(printer_id)
    if live is not None and str(getattr(live, "state", "") or "").upper() in ("RUNNING", "PAUSE", "PREPARE"):
        return True
    return (
        await db.scalar(
            select(PrintArchive.id)
            .where(
                PrintArchive.printer_id == printer_id,
                PrintArchive.status == "printing",
                PrintArchive.id != archive_id,
            )
            .limit(1)
        )
    ) is not None


async def _run_holds_its_printer(
    db, printer_id: int, archive_id: int, queue_id: int | None, run, *, live: bool = False
) -> bool:
    """The run proof the change_table claim is taken against, under the dispatch lane's lock (Codex review r1, R2):
    the printer's queue still printing exactly this run, the row the same attempt, nothing else running."""
    from backend.app.services.printer_occupancy import claim_token_matches, read_queue_occupancy

    if queue_id is None or run is None:
        return False
    queue = (await read_queue_occupancy(db, queue_id, for_update=True)).queue
    if queue.status != "printing" or queue.current_item_id != run.id:
        return False
    started_at = run.started_at
    current = await db.get(PrintQueueItem, run.id, populate_existing=True)
    if current is None or current.status != ("printing" if live else "completed") or current.archive_id != archive_id:
        return False
    if not claim_token_matches(current.started_at, started_at):
        return False
    return not await _another_run_active(db, printer_id, archive_id)


async def _swap_record_hold(db, archive_id: int, hold: str, cycle: str) -> None:
    """The print's own history: its owed table swap did not run, and why -- whoever owns the printer now."""
    from backend.app.services import swap_terminal
    from backend.app.services.archive_write_scope import locked_print

    current = await locked_print(
        db,
        archive_id,
        select(PrintArchive)
        .where(PrintArchive.id == archive_id)
        .with_for_update()
        .execution_options(populate_existing=True),
    )
    if current is not None:
        current.extra_data = {
            **dict(current.extra_data or {}),
            swap_terminal.HOLD_KEY: swap_terminal.hold_record(hold, cycle),
        }
        await db.commit()


async def _swap_settle(
    db, printer_id: int, archive_id: int, queue_id: int | None, run, *, swapped: bool, reason: str | None
) -> bool:
    """Release or hold the printer's queue -- only while this run still holds it (Codex review r1, R2).

    Under the dispatch lane's lock the queue is read again: it is this run's while it is printing this run, or a
    current job that no longer prints (none, gone, finished) with nothing running -- never while another job of its
    own holds it. A swap that ran releases it and cleans the run's row up; anything else
    pauses it with the reason on the run's row and the next job. A run that lost the queue only writes the reason on
    its own row. Returns whether the queue was this run's.
    """
    from backend.app.services.plate_hold import clean_up_finished_row
    from backend.app.services.printer_occupancy import read_queue_occupancy
    from backend.app.services.queue_counters import set_queue_idle, set_queue_paused
    from backend.app.services.queue_ops import queue_claim_scope

    if queue_id is None:
        return False
    async with queue_claim_scope(db, queue_id):
        queue = (await read_queue_occupancy(db, queue_id, for_update=True)).queue
        holder = (
            await db.get(PrintQueueItem, queue.current_item_id, populate_existing=True)
            if queue.current_item_id is not None
            else None
        )
        if queue.status != "printing" or (holder is not None and holder.status == "printing"):
            ours = False  # stopped already, or a job of its own holds it now
        elif run is not None and queue.current_item_id == run.id:
            ours = True
        else:
            # Held by nobody alive -- no current job, or one gone or finished: ours while nothing else runs.
            ours = not await _another_run_active(db, printer_id, archive_id)
        row = await _swap_run(db, archive_id, queue_id) if run is not None else None
        if swapped:
            if ours:
                await set_queue_idle(db, queue_id)
            await db.commit()
            if ours and row is not None:
                await clean_up_finished_row(db, row, queue_status="completed", plate_auto_cleared=True)
            return ours
        steer = ours or (queue.status == "paused" and queue.current_item_id is None)
        if ours:
            await set_queue_paused(db, queue_id)
        if row is not None:
            row.waiting_reason = reason
        if steer:
            nxt = (
                await db.execute(
                    select(PrintQueueItem)
                    .where(PrintQueueItem.queue_id == queue_id, PrintQueueItem.status == "pending")
                    .order_by(PrintQueueItem.position)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if nxt is not None:
                nxt.waiting_reason = reason
        await db.commit()
        return ours


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
