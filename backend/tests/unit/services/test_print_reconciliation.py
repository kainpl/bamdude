"""Tests for the startup print-reconciliation service."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import backend.app.models.printer_location  # noqa: F401 — Printer relates to it by name
from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.print_reconciliation import (
    _classify,
    _file_matches,
    _name_matches_subtask,
    _reconcile,
    _reconcile_complete_archive,
    _slicer_estimates,
    _subtask_norm,
    _subtask_stale,
)

# ---------- _classify / _file_matches (pure) ----------


def test_classify_running_same_file_is_noop():
    # Printer still printing our file — leave it alone.
    assert _classify("RUNNING", file_match=True) == "running"
    assert _classify("PAUSE", file_match=True) == "running"


def test_classify_finished_same_file_completes():
    assert _classify("FINISH", file_match=True) == "completed"
    assert _classify("IDLE", file_match=True) == "completed"


def test_classify_failed_same_file_fails():
    assert _classify("FAILED", file_match=True) == "failed"


def test_classify_no_file_match_is_uncertain():
    # Printer moved on to a different/unknown file — real outcome unknown.
    assert _classify("RUNNING", file_match=False) == "uncertain"
    assert _classify("FINISH", file_match=False) == "uncertain"
    assert _classify("FAILED", file_match=False) == "uncertain"
    assert _classify("IDLE", file_match=False) == "uncertain"


def test_classify_same_file_different_subtask_is_ghost_replay():
    # Same file still RUNNING but under a new subtask_id — a firmware replay
    # superseded the tracked print, so it's closed uncertain, not "running"
    # (#1542 follow-up).
    assert _classify("RUNNING", file_match=True, subtask_stale=True) == "uncertain"
    assert _classify("PAUSE", file_match=True, subtask_stale=True) == "uncertain"


def test_subtask_stale_requires_both_ids_known_and_differing():
    assert _subtask_stale("A", "B") is True
    assert _subtask_stale("A", "A") is False
    # Missing on either side is ambiguous — never forces a close.
    assert _subtask_stale(None, "B") is False
    assert _subtask_stale("A", "") is False
    assert _subtask_stale("", "") is False
    assert _subtask_stale("  A ", "A") is False  # whitespace-normalised


def test_file_matches_tolerates_path_and_extension():
    assert _file_matches("widget.3mf", "widget.3mf") is True
    assert _file_matches("widget.3mf", "ftp:///cache/widget.gcode.3mf") is True
    assert _file_matches("Widget.3mf", "widget") is True
    assert _file_matches("widget.3mf", "other.3mf") is False
    assert _file_matches("", "widget.3mf") is False
    assert _file_matches("widget.3mf", "") is False


# ---------- _subtask_norm / _name_matches_subtask (pure) ----------


def test_subtask_norm_strips_sliced_extensions_and_normalises():
    assert _subtask_norm("2-foo-bar.gcode.3mf") == "2-foo-bar"
    assert _subtask_norm("  Widget.3mf ") == "widget"
    assert _subtask_norm("/data/Metadata/plate_1.gcode") == "plate_1"
    assert _subtask_norm("plain-name") == "plain-name"
    # Middle dots are preserved (human labels), unlike _file_matches' _stem.
    assert _subtask_norm("v1.2-part") == "v1.2-part"
    assert _subtask_norm("") == ""


def _archive_stub(print_name="", filename=""):
    return PrintArchive(printer_id=1, file_path="", file_size=0, print_name=print_name, filename=filename)


def test_name_matches_subtask_matches_print_name_or_filename_stem():
    # H2/X-series: gcode_file is generic, but subtask_name matches print_name.
    a = _archive_stub(print_name="povitriano-viddilennia", filename="povitriano-viddilennia.gcode.3mf")
    assert _name_matches_subtask(a, "povitriano-viddilennia") is True
    assert _name_matches_subtask(a, "POVITRIANO-VIDDILENNIA") is True  # case-insensitive
    assert _name_matches_subtask(a, "povitriano-viddilennia.gcode.3mf") is True  # extension-tolerant
    # A user-renamed print_name still matches via the sliced filename stem.
    b = _archive_stub(print_name="Custom Title", filename="povitriano-viddilennia.gcode.3mf")
    assert _name_matches_subtask(b, "povitriano-viddilennia") is True


def test_name_matches_subtask_rejects_mismatch_and_empty():
    a = _archive_stub(print_name="foo", filename="foo.3mf")
    assert _name_matches_subtask(a, "bar") is False
    # An empty live subtask is ambiguous — never a match.
    assert _name_matches_subtask(a, "") is False
    assert _name_matches_subtask(a, "   ") is False


def test_a_file_named_only_by_its_extension_still_matches_itself():
    """Regression, from the farm on 2026-09-06.

    A library file was called just ``.gcode.3mf``. The dispatcher uploads the
    stem plus ``.3mf`` — here ``/.3mf`` — and the X2D echoed ``subtask: .3mf``.
    Both sides normalise to the empty string, and the matcher read that empty
    string as "printer between jobs" and refused. The completion then left the
    queue row in ``printing`` for good (only the claim was released), and one
    reconnect during the seven-hour print would have closed it as completed.

    Emptiness is decided on what the printer actually SAID, not on what is left
    after the extensions are stripped: a printer between jobs echoes nothing.
    """
    a = _archive_stub(print_name="", filename=".gcode.3mf")
    assert _name_matches_subtask(a, ".3mf") is True
    assert _name_matches_subtask(a, ".gcode.3mf") is True
    # The raw-empty guard is untouched — an idle printer still matches nothing.
    assert _name_matches_subtask(a, "") is False
    # And an archive that recorded no name at all is not "the same empty name".
    nameless = _archive_stub(print_name="", filename="")
    assert _name_matches_subtask(nameless, ".3mf") is False


def test_subtask_norm_folds_spaces_onto_underscores():
    """The printer echoes ``subtask_name`` as the sanitised file stem, spaces
    turned into underscores; the archive keeps the name as uploaded."""
    assert _subtask_norm("Rear Dry Pod") == _subtask_norm("Rear_Dry_Pod")
    assert _subtask_norm("a b.gcode.3mf") == "a_b"
    # Folding must not disturb anything that had no spaces to begin with.
    assert _subtask_norm("W76622_DA_m4t-batholder-vb.1.1_x6") == "w76622_da_m4t-batholder-vb.1.1_x6"


def test_one_space_no_longer_closes_a_running_print():
    """Regression, from the live incident on 2026-08-17.

    An X2D two hours into a job reported::

        subtask: AMS_2_Pro_Dry_Pods_–_Modular_Desiccant_System_Rear_Dry_Pod

    against an archive filename ending ``…_Rear Dry Pod.gcode.3mf`` — identical
    but for one space. H2/X-series firmware also hides the real filename behind
    ``/data/Metadata/plate_5.gcode``, so with the fallback missing there was
    nothing left to match on and the print was closed as completed on restart.
    Four A1 Minis printing the same evening survived only because their filename
    contains no spaces.
    """
    live_subtask = "AMS_2_Pro_Dry_Pods_–_Modular_Desiccant_System_Rear_Dry_Pod"
    a = _archive_stub(
        print_name="AMS 2 Pro Dry Pods – Modular Desiccant System - Plate 5",
        filename="AMS_2_Pro_Dry_Pods_–_Modular_Desiccant_System_Rear Dry Pod.gcode.3mf",
    )

    assert _file_matches(a.filename, "/data/Metadata/plate_5.gcode") is False, (
        "premise: the generic gcode path is why the subtask fallback exists at all"
    )
    assert _name_matches_subtask(a, live_subtask) is True

    # ⚠️ It matches on the FILENAME. ``print_name`` carries a " - Plate N"
    # suffix on every multi-plate job and can never equal a subtask — which is
    # why the filename candidate must keep working and must not be dropped.
    b = _archive_stub(print_name=a.print_name, filename="")
    assert _name_matches_subtask(b, live_subtask) is False


# ---------- _slicer_estimates (pure, best-effort) ----------


def test_slicer_estimates_missing_file_returns_empty():
    assert _slicer_estimates("") == {}
    assert _slicer_estimates("/no/such/file.3mf") == {}


def test_slicer_estimates_unreadable_file_returns_empty(tmp_path):
    # A non-3MF file must not raise — best-effort means best-effort.
    junk = tmp_path / "not.3mf"
    junk.write_bytes(b"not a zip")
    assert _slicer_estimates(str(junk)) == {}


def test_slicer_estimates_resolves_relative_path_against_base_dir(tmp_path, monkeypatch):
    """``PrintArchive.file_path`` is stored relative to ``settings.base_dir``, so
    the raw string must never be handed to the filesystem as-is — it would
    resolve against the process CWD and the estimate would silently never fire.
    """
    from backend.app.services import print_reconciliation as mod

    (tmp_path / "20250101_000000_widget").mkdir()
    target = tmp_path / "20250101_000000_widget" / "widget.gcode.3mf"
    target.write_bytes(b"not a zip")  # existence is what we're pinning
    monkeypatch.setattr(mod.settings, "base_dir", tmp_path)

    seen = {}

    class _Parser:
        def __init__(self, file_path, plate_number=None):
            seen["path"] = file_path
            seen["plate"] = plate_number

        def parse(self):
            return {"print_time_seconds": 900, "filament_used_grams": 12.5}

    monkeypatch.setattr("backend.app.services.archive.ThreeMFParser", _Parser)

    out = _slicer_estimates("20250101_000000_widget/widget.gcode.3mf")

    assert out == {"print_time_seconds": 900, "filament_used_grams": 12.5}
    assert seen["path"] == target


def test_slicer_estimates_scopes_the_parse_to_the_printed_plate(tmp_path, monkeypatch):
    """Without a plate number the parser falls back to the first ``<plate>``, so
    a recovered print of plate 5 would inherit plate 1's weight and time."""
    from backend.app.services import print_reconciliation as mod

    target = tmp_path / "multi.gcode.3mf"
    target.write_bytes(b"not a zip")
    monkeypatch.setattr(mod.settings, "base_dir", tmp_path)

    seen = {}

    class _Parser:
        def __init__(self, file_path, plate_number=None):
            seen["plate"] = plate_number

        def parse(self):
            return {}

    monkeypatch.setattr("backend.app.services.archive.ThreeMFParser", _Parser)

    _slicer_estimates("multi.gcode.3mf", 5)
    assert seen["plate"] == 5


# ---------- _reconcile_complete_archive (DB) ----------


async def _make_archive(db, **overrides):
    # Aged by default: this factory exists to build *orphans*, and a real orphan
    # is a print the app lost track of — minutes or hours old, never seconds.
    # The sweep now skips rows created within _JUST_DISPATCHED_SECONDS, so a
    # "just created" default would quietly make every orphan test a no-op.
    created_at = overrides.get("created_at", datetime.now(timezone.utc) - timedelta(hours=2))
    archive = PrintArchive(
        printer_id=overrides.get("printer_id", 1),
        filename=overrides.get("filename", "widget.3mf"),
        file_path=overrides.get("file_path", ""),
        file_size=0,
        print_name=overrides.get("print_name"),
        status="printing",
        started_at=overrides.get("started_at", datetime.now(timezone.utc)),
        print_time_seconds=overrides.get("print_time_seconds"),
        filament_used_grams=overrides.get("filament_used_grams"),
        subtask_id=overrides.get("subtask_id"),
    )
    archive.created_at = created_at.replace(tzinfo=None) if created_at.tzinfo else created_at
    db.add(archive)
    await db.flush()
    return archive


@pytest.mark.asyncio
async def test_reconcile_complete_closes_archive_completed(db_session):
    archive = await _make_archive(db_session)
    await _reconcile_complete_archive(db_session, archive, status="completed", uncertain=False)
    assert archive.status == "completed"
    assert archive.completed_at is not None
    assert archive.extra_data["recovered_by_startup_sweep"] is True
    assert "recovered_outcome_uncertain" not in archive.extra_data


@pytest.mark.asyncio
async def test_reconcile_complete_uncertain_sets_flag(db_session):
    archive = await _make_archive(db_session)
    await _reconcile_complete_archive(db_session, archive, status="completed", uncertain=True)
    assert archive.status == "completed"
    assert archive.extra_data["recovered_outcome_uncertain"] is True


@pytest.mark.asyncio
async def test_reconcile_complete_advances_queue_item(db_session):
    queue = PrinterQueue(printer_id=1, status="printing")
    db_session.add(queue)
    await db_session.flush()
    archive = await _make_archive(db_session)
    item = PrintQueueItem(queue_id=queue.id, archive_id=archive.id, status="printing")
    db_session.add(item)
    await db_session.flush()

    await _reconcile_complete_archive(db_session, archive, status="completed", uncertain=False)

    assert item.status == "completed"
    assert item.completed_at is not None
    assert queue.status == "idle"


@pytest.mark.asyncio
async def test_reconcile_complete_failed_sets_queue_error(db_session):
    queue = PrinterQueue(printer_id=1, status="printing")
    db_session.add(queue)
    await db_session.flush()
    archive = await _make_archive(db_session)
    item = PrintQueueItem(queue_id=queue.id, archive_id=archive.id, status="printing")
    db_session.add(item)
    await db_session.flush()

    await _reconcile_complete_archive(db_session, archive, status="failed", uncertain=False)

    assert item.status == "failed"
    assert queue.status == "error"


@pytest.mark.asyncio
async def test_reconcile_complete_no_queue_item_is_fine(db_session):
    # External / Send-to-Printer archive with no linked queue item.
    archive = await _make_archive(db_session)
    await _reconcile_complete_archive(db_session, archive, status="completed", uncertain=False)
    assert archive.status == "completed"


# ---------- completed_at is a reconstruction, not the reconnect moment (#2592) ----------


@pytest.mark.asyncio
async def test_reconcile_does_not_bank_the_disconnect_gap(db_session):
    """A 3-day outage must not become 3 days of print time. The closure lands on
    the slicer's predicted natural end, so both the stored duration and the
    stats total see 2h — the print's real length — not the gap."""
    started = datetime.now(timezone.utc) - timedelta(days=3)
    archive = await _make_archive(db_session, started_at=started, print_time_seconds=7200)

    await _reconcile_complete_archive(db_session, archive, status="completed", uncertain=False)

    assert (archive.completed_at - archive.started_at).total_seconds() == 7200
    await db_session.flush()
    assert archive.actual_time_seconds == 7200


@pytest.mark.asyncio
async def test_reconcile_without_estimate_contributes_zero(db_session):
    """No slicer estimate = no evidence of how long it ran. Contributing nothing
    is the honest answer (upstream stores duration_seconds=0 here)."""
    started = datetime.now(timezone.utc) - timedelta(days=2)
    archive = await _make_archive(db_session, started_at=started, print_time_seconds=None)

    await _reconcile_complete_archive(db_session, archive, status="completed", uncertain=False)

    assert archive.completed_at == archive.started_at
    await db_session.flush()
    # compute_time_metrics returns None for a non-positive duration.
    assert archive.actual_time_seconds is None


@pytest.mark.asyncio
async def test_reconcile_never_stamps_a_finish_in_the_future(db_session):
    """Short downtime, long estimate: the predicted end is still ahead of us, so
    it clamps to now rather than dating the print into the future."""
    started = datetime.now(timezone.utc) - timedelta(seconds=60)
    archive = await _make_archive(db_session, started_at=started, print_time_seconds=28800)

    await _reconcile_complete_archive(db_session, archive, status="completed", uncertain=False)

    elapsed = (archive.completed_at - archive.started_at).total_seconds()
    assert 55 <= elapsed <= 120  # ~now, not started_at + 8h


@pytest.mark.asyncio
async def test_reconcile_failed_records_why_the_row_was_closed(db_session):
    archive = await _make_archive(db_session)
    await _reconcile_complete_archive(db_session, archive, status="failed", uncertain=False)
    assert archive.failure_reason == "Stale - reconciled after reconnect, end time unknown"


@pytest.mark.asyncio
async def test_reconcile_keeps_an_existing_failure_reason(db_session):
    # An HMS-classified fault already explains itself — don't overwrite it.
    archive = await _make_archive(db_session)
    archive.failure_reason = "Nozzle clog"
    await _reconcile_complete_archive(db_session, archive, status="failed", uncertain=False)
    assert archive.failure_reason == "Nozzle clog"


# ---------- _reconcile (orchestrator, DB) ----------


@pytest.mark.asyncio
async def test_reconcile_running_same_file_is_left_alone(db_session):
    archive = await _make_archive(db_session, filename="widget.3mf")
    await _reconcile(db_session, printer_id=1, live_state="RUNNING", live_file="widget.3mf")
    assert archive.status == "printing"  # still printing — untouched


@pytest.mark.asyncio
async def test_reconcile_same_subtask_still_running_is_left_alone(db_session):
    # Same file, same subtask, still RUNNING — genuinely in progress; untouched.
    archive = await _make_archive(db_session, filename="widget.3mf", subtask_id="task-1")
    await _reconcile(db_session, printer_id=1, live_state="RUNNING", live_file="widget.3mf", live_subtask_id="task-1")
    assert archive.status == "printing"


@pytest.mark.asyncio
async def test_reconcile_ghost_replay_new_subtask_closes_uncertain(db_session):
    # Same file still RUNNING but under a new subtask_id — a firmware replay
    # superseded the tracked print; close it uncertain instead of leaving it
    # stuck at "printing" forever (#1542 follow-up).
    archive = await _make_archive(db_session, filename="widget.3mf", subtask_id="task-1")
    await _reconcile(db_session, printer_id=1, live_state="RUNNING", live_file="widget.3mf", live_subtask_id="task-2")
    assert archive.status == "completed"
    assert archive.extra_data["recovered_outcome_uncertain"] is True


@pytest.mark.asyncio
async def test_reconcile_bare_connect_unknown_state_leaves_orphans(db_session):
    # Pre-push_status degenerate state ("unknown" / "") is not evidence a print
    # ended — the sweep must no-op, not synthesise completions (#1679 parity).
    archive = await _make_archive(db_session, filename="widget.3mf", subtask_id="task-1")
    await _reconcile(db_session, printer_id=1, live_state="unknown", live_file="", live_subtask_id="")
    assert archive.status == "printing"
    await _reconcile(db_session, printer_id=1, live_state="", live_file="widget.3mf", live_subtask_id="task-1")
    assert archive.status == "printing"


@pytest.mark.asyncio
async def test_reconcile_finished_during_downtime_completes(db_session):
    archive = await _make_archive(db_session, filename="widget.3mf")
    await _reconcile(db_session, printer_id=1, live_state="FINISH", live_file="widget.3mf")
    assert archive.status == "completed"
    assert archive.extra_data["recovered_by_startup_sweep"] is True


@pytest.mark.asyncio
async def test_reconcile_printer_moved_on_is_uncertain(db_session):
    archive = await _make_archive(db_session, filename="widget.3mf")
    await _reconcile(db_session, printer_id=1, live_state="RUNNING", live_file="other.3mf")
    assert archive.status == "completed"
    assert archive.extra_data["recovered_outcome_uncertain"] is True


@pytest.mark.asyncio
async def test_reconcile_failed_state_fails_the_archive(db_session):
    archive = await _make_archive(db_session, filename="widget.3mf")
    await _reconcile(db_session, printer_id=1, live_state="FAILED", live_file="widget.3mf")
    assert archive.status == "failed"


@pytest.mark.asyncio
async def test_reconcile_only_touches_this_printer(db_session):
    mine = await _make_archive(db_session, printer_id=1, filename="widget.3mf")
    other = await _make_archive(db_session, printer_id=2, filename="widget.3mf")
    await _reconcile(db_session, printer_id=1, live_state="FINISH", live_file="widget.3mf")
    assert mine.status == "completed"
    assert other.status == "printing"  # different printer — untouched


@pytest.mark.asyncio
async def test_reconcile_is_idempotent(db_session):
    archive = await _make_archive(db_session, filename="widget.3mf")
    await _reconcile(db_session, printer_id=1, live_state="FINISH", live_file="widget.3mf")
    await _reconcile(db_session, printer_id=1, live_state="FINISH", live_file="widget.3mf")
    assert archive.status == "completed"  # second run is a harmless no-op


# ---------- H2/X-series generic gcode_file (subtask-name fallback) ----------


@pytest.mark.asyncio
async def test_reconcile_h2x_generic_file_running_subtask_match_is_left_alone(db_session):
    # H2/X-series firmware reports gcode_file as /data/Metadata/plate_1.gcode,
    # which never stem-matches the sliced filename. Without the subtask-name
    # fallback the ACTIVE print gets closed as uncertain on every reconnect
    # (duplicate archive + false completion — the X2D bug). With it: left alone.
    archive = await _make_archive(db_session, filename="povitriano.gcode.3mf", print_name="povitriano")
    await _reconcile(
        db_session,
        printer_id=1,
        live_state="RUNNING",
        live_file="/data/Metadata/plate_1.gcode",
        live_subtask_name="povitriano",
    )
    assert archive.status == "printing"  # NOT closed


@pytest.mark.asyncio
async def test_reconcile_h2x_generic_file_finished_subtask_match_completes_clean(db_session):
    # Genuinely finished during downtime: generic file + matching subtask + FINISH
    # → closed as completed (certain), not uncertain.
    archive = await _make_archive(db_session, filename="povitriano.gcode.3mf", print_name="povitriano")
    await _reconcile(
        db_session,
        printer_id=1,
        live_state="FINISH",
        live_file="/data/Metadata/plate_1.gcode",
        live_subtask_name="povitriano",
    )
    assert archive.status == "completed"
    assert "recovered_outcome_uncertain" not in (archive.extra_data or {})


@pytest.mark.asyncio
async def test_reconcile_h2x_generic_file_different_subtask_is_uncertain(db_session):
    # Generic file AND no subtask match — the printer moved on to a different
    # job; the tracked print is a real orphan → close it uncertain.
    archive = await _make_archive(db_session, filename="povitriano.gcode.3mf", print_name="povitriano")
    await _reconcile(
        db_session,
        printer_id=1,
        live_state="RUNNING",
        live_file="/data/Metadata/plate_1.gcode",
        live_subtask_name="different-job",
    )
    assert archive.status == "completed"
    assert archive.extra_data["recovered_outcome_uncertain"] is True


# ---------- dispatch-in-flight guard (the operator's vanishing queue items) ----------


class TestADispatchInFlightIsNotAnOrphan:
    """The sweep re-arms on every MQTT client recreation (#1542) so a print that
    ended during a disconnect still gets closed. On one operator's swap farm the
    MQTT link went stale every ~45 minutes — almost exactly its print cycle — so
    reconnects kept landing in the seconds between "archive created for the job
    we just sent" and "printer starts printing it".

    In that window the printer still reports the PREVIOUS job's FINISH, and
    because the farm reprints one file the filename matches, so the sweep called
    a job that had not begun "completed" and closed it, taking its queue item
    with it. The printer then printed the file for its full 43 minutes. From the
    outside the queue emptied itself without producing parts.
    """

    @staticmethod
    def _hold(printer_id: int, age_seconds: float = 0.0):
        """Put the printer in a post-dispatch hold, as the dispatcher does."""
        import time

        from backend.app.services.print_scheduler import scheduler

        scheduler._dispatch_holds[printer_id] = (time.monotonic() - age_seconds, "FINISH", "sub-1")

    @staticmethod
    def _clear(printer_id: int):
        from backend.app.services.print_scheduler import scheduler

        scheduler._dispatch_holds.pop(printer_id, None)

    @pytest.mark.asyncio
    async def test_the_operators_timeline_no_hold_yet_just_a_fresh_archive(self, db_session):
        """The real collision, reproduced.

        The dispatch hold is only registered once the print command has landed
        (``_mark_printer_dispatched`` runs after a successful dispatch), and the
        sweep fires earlier than that: the dispatcher refreshes a stale MQTT
        link *before* uploading, the reconnect re-arms this sweep, and the first
        push arrives while FTP is still running. In the operator's log the
        archive was closed 0.9 s after it was created and 1m44s before its print
        began — with no hold in place at any point.
        """
        archive = await _make_archive(db_session, printer_id=11, filename="nose_110mm.3mf")
        archive.created_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await db_session.flush()
        self._clear(11)  # no dispatch hold — exactly as it was

        await _reconcile(db_session, 11, live_state="FINISH", live_file="nose_110mm.3mf")

        assert archive.status == "printing", "a job created a second ago has not had time to be an orphan"

    @pytest.mark.asyncio
    async def test_an_archive_older_than_the_window_is_still_closed(self, db_session):
        """The guard is a window, not an amnesty: a print interrupted by a
        restart must still be reconciled."""
        from backend.app.services.print_reconciliation import _JUST_DISPATCHED_SECONDS

        archive = await _make_archive(db_session, printer_id=12, filename="nose_110mm.3mf")
        archive.created_at = (datetime.now(timezone.utc) - timedelta(seconds=_JUST_DISPATCHED_SECONDS + 60)).replace(
            tzinfo=None
        )
        await db_session.flush()
        self._clear(12)

        await _reconcile(db_session, 12, live_state="FINISH", live_file="nose_110mm.3mf")

        assert archive.status == "completed"

    @pytest.mark.asyncio
    async def test_an_archive_from_an_in_flight_dispatch_survives(self, db_session):
        archive = await _make_archive(db_session, printer_id=7, filename="nose_110mm.3mf")
        self._hold(7)
        try:
            # Exactly the operator's situation: same file every cycle, so the name
            # matches, and the printer is still FINISH from the print before.
            await _reconcile(db_session, 7, live_state="FINISH", live_file="nose_110mm.3mf")
        finally:
            self._clear(7)

        assert archive.status == "printing", "a job that has not started is not an orphan"

    @pytest.mark.asyncio
    async def test_a_genuine_orphan_is_still_closed(self, db_session):
        """The guard must not disarm the sweep — that would strand every print
        interrupted by a restart."""
        archive = await _make_archive(db_session, printer_id=8, filename="nose_110mm.3mf")
        self._clear(8)

        await _reconcile(db_session, 8, live_state="FINISH", live_file="nose_110mm.3mf")

        assert archive.status == "completed"

    @pytest.mark.asyncio
    async def test_a_stale_hold_does_not_protect_forever(self, db_session):
        """A hold that outlived its window must not become a permanent excuse to
        skip reconciliation for that printer."""
        from backend.app.services.print_scheduler import scheduler

        archive = await _make_archive(db_session, printer_id=9, filename="nose_110mm.3mf")
        self._hold(9, age_seconds=scheduler._dispatch_max_hold + 60)
        try:
            await _reconcile(db_session, 9, live_state="FINISH", live_file="nose_110mm.3mf")
        finally:
            self._clear(9)

        assert archive.status == "completed"


# --- Recovered prints do the completion bookkeeping too --------------------
#
# A print that ended while the process was down is still a finished print. The
# sweep used to close the archive and stop there, so the library counters and
# the energy figure stayed at whatever they were when the machine died.


async def _make_library_file(db, **overrides):
    lib = LibraryFile(
        filename=overrides.get("filename", "widget.3mf"),
        file_path=overrides.get("file_path", "lib/widget.3mf"),
        file_type=overrides.get("file_type", "3mf"),
        file_size=overrides.get("file_size", 1),
        print_count=overrides.get("print_count", 0),
    )
    db.add(lib)
    await db.flush()
    return lib


@pytest.mark.asyncio
async def test_a_recovered_completion_bumps_the_library_counters(db_session):
    lib = await _make_library_file(db_session)
    archive = await _make_archive(db_session)
    archive.library_file_id = lib.id
    await db_session.flush()

    await _reconcile_complete_archive(db_session, archive, status="completed", uncertain=False)

    assert lib.print_count == 1
    assert lib.last_printed_at is not None


@pytest.mark.asyncio
async def test_a_recovered_failure_does_not_bump_the_library_counters(db_session):
    """Parity with the live handler: successes only."""
    lib = await _make_library_file(db_session)
    archive = await _make_archive(db_session)
    archive.library_file_id = lib.id
    await db_session.flush()

    await _reconcile_complete_archive(db_session, archive, status="failed", uncertain=False)

    assert lib.print_count == 0
    assert lib.last_printed_at is None


@pytest.mark.asyncio
async def test_an_archive_with_no_library_file_bumps_nothing(db_session):
    archive = await _make_archive(db_session)
    await _reconcile_complete_archive(db_session, archive, status="completed", uncertain=False)
    assert archive.status == "completed"


@pytest.mark.asyncio
async def test_the_sweep_reports_which_archives_it_recovered(db_session):
    """The energy read happens after the commit, so the caller needs the ids."""
    archive = await _make_archive(db_session)
    recovered = await _reconcile_complete_archive(db_session, archive, status="completed", uncertain=False)
    assert recovered == archive.id
