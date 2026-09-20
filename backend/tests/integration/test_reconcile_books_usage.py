"""A print the startup sweep closes books its filament like a supervised one.

Measured hole 2026-08-29: the PC rebooted overnight, six prints finished
while BamDude was down, the sweep closed their archives as completed — and
none of them booked a gram. ~1.77 kg passed the books by.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

import backend.app.models.printer_location  # noqa: F401 — resolves Printer's rel
from backend.app.models.archive import PrintArchive
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services.print_reconciliation import _reconcile_complete_archive

pytestmark = pytest.mark.integration


async def _orphan_archive(db, printer_factory, status="printing"):
    printer = await printer_factory()
    archive = PrintArchive(
        printer_id=printer.id,
        filename="job.gcode.3mf",
        print_name="Overnight_Job",
        file_path="x/job.gcode.3mf",
        file_size=1,
        status=status,
        started_at=datetime.now(timezone.utc),
    )
    db.add(archive)
    await db.commit()
    await db.refresh(archive)
    return printer, archive


@pytest.mark.asyncio
async def test_a_completed_close_books_usage(db_session, printer_factory):
    printer, archive = await _orphan_archive(db_session, printer_factory)

    with patch("backend.app.services.usage_tracker.on_print_complete", new_callable=AsyncMock) as mock_book:
        await _reconcile_complete_archive(db_session, archive, status="completed", uncertain=False)

    mock_book.assert_awaited_once()
    kwargs = mock_book.await_args.kwargs
    assert kwargs["archive_id"] == archive.id
    assert kwargs["expected_print_name"] == "Overnight_Job"


@pytest.mark.asyncio
async def test_a_failed_close_books_nothing(db_session, printer_factory):
    """A reconciled failure carries no layer information — booking the full
    estimate for a partial print would be worse than the gap."""
    printer, archive = await _orphan_archive(db_session, printer_factory)

    with patch("backend.app.services.usage_tracker.on_print_complete", new_callable=AsyncMock) as mock_book:
        await _reconcile_complete_archive(db_session, archive, status="failed", uncertain=True)

    mock_book.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_already_booked_archive_is_not_double_booked(db_session, printer_factory):
    printer, archive = await _orphan_archive(db_session, printer_factory)
    db_session.add(
        SpoolUsageHistory(
            spool_id=1,  # FK unenforced on SQLite; NOT NULL is the constraint that matters
            printer_id=printer.id,
            archive_id=archive.id,
            weight_used=10.0,
            status="completed",
        )
    )
    await db_session.commit()

    with patch("backend.app.services.usage_tracker.on_print_complete", new_callable=AsyncMock) as mock_book:
        await _reconcile_complete_archive(db_session, archive, status="completed", uncertain=False)

    mock_book.assert_not_awaited()


class TestTheSessionNameGuard:
    """``expected_print_name`` inside the tracker itself."""

    @pytest.mark.asyncio
    async def test_a_mismatched_session_is_put_back_untouched(self, db_session):
        from backend.app.services import usage_tracker as ut

        sentinel = ut.PrintSession(
            printer_id=901,
            print_name="Next_Job",
            started_at=datetime.now(timezone.utc),
        )
        ut._active_sessions[901] = sentinel
        try:
            with patch.object(ut, "restore_session", new_callable=AsyncMock):
                await ut.on_print_complete(
                    901,
                    {"status": "completed"},
                    printer_manager=None,
                    db=db_session,
                    archive_id=None,
                    expected_print_name="Overnight_Job",
                )
        except Exception:
            pass  # downstream booking may fail on the bare fixture — the guard is the subject
        assert ut._active_sessions.get(901) is sentinel, (
            "a session for a different print must survive a reconcile-driven completion"
        )
        ut._active_sessions.pop(901, None)
