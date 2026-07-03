"""Tests for the startup print-reconciliation service."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.print_reconciliation import (
    _classify,
    _file_matches,
    _reconcile,
    _reconcile_complete_archive,
    _slicer_estimates,
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


# ---------- _slicer_estimates (pure, best-effort) ----------


def test_slicer_estimates_missing_file_returns_empty():
    assert _slicer_estimates("") == {}
    assert _slicer_estimates("/no/such/file.3mf") == {}


def test_slicer_estimates_unreadable_file_returns_empty(tmp_path):
    # A non-3MF file must not raise — best-effort means best-effort.
    junk = tmp_path / "not.3mf"
    junk.write_bytes(b"not a zip")
    assert _slicer_estimates(str(junk)) == {}


# ---------- _reconcile_complete_archive (DB) ----------


async def _make_archive(db, **overrides):
    archive = PrintArchive(
        printer_id=overrides.get("printer_id", 1),
        filename=overrides.get("filename", "widget.3mf"),
        file_path=overrides.get("file_path", ""),
        file_size=0,
        status="printing",
        started_at=datetime.now(timezone.utc),
        print_time_seconds=overrides.get("print_time_seconds"),
        filament_used_grams=overrides.get("filament_used_grams"),
        subtask_id=overrides.get("subtask_id"),
    )
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
