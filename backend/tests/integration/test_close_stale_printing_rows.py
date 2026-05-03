"""Regression tests for ``_close_stale_printing_rows`` cleanup helper.

The helper runs at the top of ``on_print_start`` and closes stale ``status='printing'``
archive rows on the printer that fired the event:

* Different ``filename`` from the current event → close (predicted_end<now → completed,
  predicted_end>now → cancelled).
* Same ``filename`` but **not the newest** → close per the same rule.
* Same ``filename`` AND newest → **left alone** so the downstream name-match
  adoption block + FTP/hash flow can decide.

Closed rows get ``extra_data['recovered_by_cleanup']=True`` for audit.

Trade-off accepted: long BamDude downtime + reprint of the same file from the
printer screen could glue the new live print onto the old archive row in the
downstream adoption block. Documented in the helper docstring.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from backend.app.main import _close_stale_printing_rows
from backend.app.models.archive import PrintArchive


def _make_archive(
    *,
    printer_id: int,
    filename: str,
    started_at: datetime | None,
    print_time_seconds: int | None,
    status: str = "printing",
    completed_at: datetime | None = None,
) -> PrintArchive:
    """Helper to build an in-flight ``PrintArchive`` row with sensible defaults."""
    return PrintArchive(
        printer_id=printer_id,
        filename=filename,
        file_path=f"/tmp/{filename}",
        file_size=1024,
        print_name=filename.replace(".gcode.3mf", "").replace(".3mf", ""),
        status=status,
        started_at=started_at,
        completed_at=completed_at,
        print_time_seconds=print_time_seconds,
        content_hash=f"hash-{filename}",
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_other_filename_predicted_past_marked_completed(db_session, printer_factory):
    """Stale row for a *different* file with predicted_end < now → completed."""
    printer = await printer_factory()
    now = datetime.now(timezone.utc)

    stale = _make_archive(
        printer_id=printer.id,
        filename="old_print.gcode.3mf",
        started_at=now - timedelta(hours=10),
        print_time_seconds=3600,  # 1h job, started 10h ago → predicted_end 9h ago
    )
    db_session.add(stale)
    await db_session.commit()
    await db_session.refresh(stale)
    stale_id = stale.id

    await _close_stale_printing_rows(
        printer.id,
        "different_file",
        db_session,
        logging.getLogger("test"),
    )

    await db_session.refresh(stale)
    assert stale.status == "completed"
    assert stale.completed_at is not None
    assert stale.extra_data == {"recovered_by_cleanup": True}
    # completed_at should be roughly started_at + print_time
    expected_end = (now - timedelta(hours=10)) + timedelta(seconds=3600)
    completed_at_aware = (
        stale.completed_at if stale.completed_at.tzinfo else stale.completed_at.replace(tzinfo=timezone.utc)
    )
    assert abs((completed_at_aware - expected_end).total_seconds()) < 5

    _ = stale_id  # silence unused warning


@pytest.mark.asyncio
@pytest.mark.integration
async def test_other_filename_predicted_future_marked_cancelled(db_session, printer_factory):
    """Stale row for a *different* file with predicted_end > now → cancelled.

    Printer fired ``on_print_start`` for a NEW filename, so the previous one
    must have been interrupted before its slicer-predicted natural end.
    """
    printer = await printer_factory()
    now = datetime.now(timezone.utc)

    stale = _make_archive(
        printer_id=printer.id,
        filename="long_print.gcode.3mf",
        started_at=now - timedelta(hours=1),
        print_time_seconds=10 * 3600,  # 10h job started 1h ago → predicted end 9h in future
    )
    db_session.add(stale)
    await db_session.commit()
    await db_session.refresh(stale)

    await _close_stale_printing_rows(
        printer.id,
        "different_file",
        db_session,
        logging.getLogger("test"),
    )

    await db_session.refresh(stale)
    assert stale.status == "cancelled"
    assert stale.completed_at is None  # cancelled rows don't get a synthetic completed_at
    assert stale.extra_data == {"recovered_by_cleanup": True}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_same_filename_newest_not_touched(db_session, printer_factory):
    """The single same-filename row is preserved for downstream adoption."""
    printer = await printer_factory()
    now = datetime.now(timezone.utc)

    live_candidate = _make_archive(
        printer_id=printer.id,
        filename="live.gcode.3mf",
        started_at=now - timedelta(minutes=10),
        print_time_seconds=4 * 3600,  # predicted_end > now
    )
    db_session.add(live_candidate)
    await db_session.commit()
    await db_session.refresh(live_candidate)

    await _close_stale_printing_rows(
        printer.id,
        "live",
        db_session,
        logging.getLogger("test"),
    )

    await db_session.refresh(live_candidate)
    assert live_candidate.status == "printing"
    assert live_candidate.completed_at is None
    assert live_candidate.extra_data is None  # not marked


@pytest.mark.asyncio
@pytest.mark.integration
async def test_same_filename_older_siblings_closed(db_session, printer_factory):
    """Same-filename older rows close; newest is left alone."""
    printer = await printer_factory()
    now = datetime.now(timezone.utc)

    # Old + clearly past predicted_end
    old = _make_archive(
        printer_id=printer.id,
        filename="recurring.gcode.3mf",
        started_at=now - timedelta(hours=10),
        print_time_seconds=3600,
    )
    # Newer but still predicted past
    middle = _make_archive(
        printer_id=printer.id,
        filename="recurring.gcode.3mf",
        started_at=now - timedelta(hours=5),
        print_time_seconds=3600,
    )
    # Newest — predicted still printing
    newest = _make_archive(
        printer_id=printer.id,
        filename="recurring.gcode.3mf",
        started_at=now - timedelta(minutes=5),
        print_time_seconds=2 * 3600,
    )
    db_session.add_all([old, middle, newest])
    await db_session.commit()
    for row in (old, middle, newest):
        await db_session.refresh(row)

    await _close_stale_printing_rows(
        printer.id,
        "recurring",
        db_session,
        logging.getLogger("test"),
    )

    await db_session.refresh(old)
    await db_session.refresh(middle)
    await db_session.refresh(newest)

    assert old.status == "completed"
    assert old.completed_at is not None
    assert old.extra_data == {"recovered_by_cleanup": True}

    assert middle.status == "completed"
    assert middle.completed_at is not None
    assert middle.extra_data == {"recovered_by_cleanup": True}

    assert newest.status == "printing"
    assert newest.completed_at is None
    assert newest.extra_data is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_null_started_at_preserved(db_session, printer_factory):
    """Rows with NULL ``started_at`` can't be reasoned about → left alone."""
    printer = await printer_factory()

    stale = _make_archive(
        printer_id=printer.id,
        filename="incomplete_data.gcode.3mf",
        started_at=None,
        print_time_seconds=3600,
    )
    db_session.add(stale)
    await db_session.commit()
    await db_session.refresh(stale)

    await _close_stale_printing_rows(
        printer.id,
        "different",
        db_session,
        logging.getLogger("test"),
    )

    await db_session.refresh(stale)
    assert stale.status == "printing"  # untouched
    assert stale.extra_data is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_null_print_time_preserved(db_session, printer_factory):
    """Rows with NULL ``print_time_seconds`` are left alone too."""
    printer = await printer_factory()
    now = datetime.now(timezone.utc)

    stale = _make_archive(
        printer_id=printer.id,
        filename="no_estimate.gcode.3mf",
        started_at=now - timedelta(hours=24),
        print_time_seconds=None,
    )
    db_session.add(stale)
    await db_session.commit()
    await db_session.refresh(stale)

    await _close_stale_printing_rows(
        printer.id,
        "different",
        db_session,
        logging.getLogger("test"),
    )

    await db_session.refresh(stale)
    assert stale.status == "printing"  # untouched
    assert stale.extra_data is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_only_this_printer_affected(db_session, printer_factory):
    """Cleanup is scoped to the printer that fired ``on_print_start``."""
    printer_a = await printer_factory(name="A")
    printer_b = await printer_factory(name="B")
    now = datetime.now(timezone.utc)

    a_stale = _make_archive(
        printer_id=printer_a.id,
        filename="other.gcode.3mf",
        started_at=now - timedelta(hours=10),
        print_time_seconds=3600,
    )
    b_stale = _make_archive(
        printer_id=printer_b.id,
        filename="other.gcode.3mf",
        started_at=now - timedelta(hours=10),
        print_time_seconds=3600,
    )
    db_session.add_all([a_stale, b_stale])
    await db_session.commit()
    await db_session.refresh(a_stale)
    await db_session.refresh(b_stale)

    await _close_stale_printing_rows(
        printer_a.id,
        "current",
        db_session,
        logging.getLogger("test"),
    )

    await db_session.refresh(a_stale)
    await db_session.refresh(b_stale)
    assert a_stale.status == "completed"  # closed
    assert b_stale.status == "printing"  # untouched (different printer)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_already_completed_row_skipped(db_session, printer_factory):
    """Rows already in a terminal status (with completed_at set) are filtered out by the WHERE clause."""
    printer = await printer_factory()
    now = datetime.now(timezone.utc)

    done = _make_archive(
        printer_id=printer.id,
        filename="done.gcode.3mf",
        started_at=now - timedelta(hours=10),
        print_time_seconds=3600,
        status="completed",
        completed_at=now - timedelta(hours=9),
    )
    db_session.add(done)
    await db_session.commit()
    await db_session.refresh(done)

    await _close_stale_printing_rows(
        printer.id,
        "current",
        db_session,
        logging.getLogger("test"),
    )

    await db_session.refresh(done)
    # Status unchanged, no audit mark — cleanup skipped it because status != 'printing'.
    assert done.status == "completed"
    assert done.extra_data is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_filename_variations_treated_as_same_print(db_session, printer_factory):
    """Regression: row stored as ``Plate_1.3mf`` must not be cleaned up when
    on_print_start fires for ``Plate_1`` (check_name) or ``Plate_1.gcode.3mf``.

    Pre-fix the cleanup helper used strict ``filename == current_filename``,
    which mismatches the legacy fallback path that stores rows as
    ``f"{print_name}.3mf"``. Result: the live in-flight row got closed as
    "different file", and the downstream name-match adoption block created a
    fresh duplicate archive instead of resuming the existing one.
    """
    printer = await printer_factory()
    now = datetime.now(timezone.utc)

    # Three rows for the SAME logical print (check_name="Plate_1") but with
    # different stored filename / print_name shapes. Cleanup must treat all of
    # them as same-name and only close the older siblings, preserving the
    # newest for downstream adoption.
    legacy_short = _make_archive(
        printer_id=printer.id,
        filename="Plate_1.3mf",
        started_at=now - timedelta(hours=10),
        print_time_seconds=3600,
    )
    legacy_short.print_name = "Plate_1"

    full_path = _make_archive(
        printer_id=printer.id,
        filename="/data/Metadata/Plate_1.gcode.3mf",
        started_at=now - timedelta(hours=5),
        print_time_seconds=3600,
    )
    full_path.print_name = "Plate_1"

    newest_live = _make_archive(
        printer_id=printer.id,
        filename="Plate_1.gcode.3mf",
        started_at=now - timedelta(minutes=10),
        print_time_seconds=4 * 3600,  # predicted_end > now
    )
    newest_live.print_name = "Plate_1"

    db_session.add_all([legacy_short, full_path, newest_live])
    await db_session.commit()
    for row in (legacy_short, full_path, newest_live):
        await db_session.refresh(row)

    await _close_stale_printing_rows(
        printer.id,
        "Plate_1",  # check_name as on_print_start would compute it
        db_session,
        logging.getLogger("test"),
    )

    await db_session.refresh(legacy_short)
    await db_session.refresh(full_path)
    await db_session.refresh(newest_live)

    # Older same-name siblings closed.
    assert legacy_short.status == "completed"
    assert legacy_short.extra_data == {"recovered_by_cleanup": True}
    assert full_path.status == "completed"
    assert full_path.extra_data == {"recovered_by_cleanup": True}
    # Newest live row preserved for downstream name-match adoption.
    assert newest_live.status == "printing"
    assert newest_live.completed_at is None
    assert newest_live.extra_data is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_no_stale_rows_no_op(db_session, printer_factory):
    """When there's nothing to close, the helper is a quiet no-op."""
    printer = await printer_factory()

    # No rows at all — should not error.
    await _close_stale_printing_rows(
        printer.id,
        "anything",
        db_session,
        logging.getLogger("test"),
    )
    # Nothing to assert — just verify no exception was raised.
