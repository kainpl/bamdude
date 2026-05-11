"""Regression tests for cancel-during-dispatch recovery.

Covers three correlated bugs fixed in the 0.4.4 cycle:

1. ``_mark_dispatch_archive_terminal`` flips a "printing" archive to a
   terminal state (regression-test for the helper itself).
2. The reprint dispatch path (``_run_reprint_archive``) used to leave a
   zombie ``status='printing'`` archive when cancelled mid-flight — the
   library-file path already flipped it to ``cancelled`` but the reprint
   path didn't. The fix added the symmetrical
   ``_mark_dispatch_archive_terminal(..., "cancelled", ...)`` call.
3. ``_cancel_item`` in the print scheduler marks the queue item as
   ``cancelled`` and the queue as ``paused`` (not ``failed`` + ``error``)
   so a user-initiated cancel doesn't look like a system failure.

These tests are intentionally small and surgical — they exercise the
specific helpers that hold the invariants, rather than spinning up a
full dispatcher loop.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services.background_dispatch import BackgroundDispatchService


@pytest.mark.asyncio
@pytest.mark.integration
async def test_mark_dispatch_archive_terminal_flips_printing_to_cancelled(db_session, archive_factory, printer_factory):
    """A 'printing' archive becomes 'cancelled' + carries the error_message + completed_at.

    Guards the actual mechanic the cancel-recovery branch relies on.
    """
    printer = await printer_factory()
    archive = await archive_factory(printer.id, status="printing", error_message=None, completed_at=None)
    archive_id = archive.id

    with patch("backend.app.services.background_dispatch.async_session") as mock_session_factory:
        mock_session_factory.return_value.__aenter__.return_value = db_session
        await BackgroundDispatchService._mark_dispatch_archive_terminal(
            archive_id, "cancelled", "Cancelled before start"
        )

    await db_session.refresh(archive)
    assert archive.status == "cancelled"
    assert archive.error_message == "Cancelled before start"
    assert archive.completed_at is not None
    # The mechanic stamps a UTC datetime; tolerate a small clock skew.
    delta = (datetime.now(timezone.utc) - archive.completed_at.replace(tzinfo=timezone.utc)).total_seconds()
    assert abs(delta) < 30


@pytest.mark.asyncio
@pytest.mark.integration
async def test_mark_dispatch_archive_terminal_idempotent_on_non_printing(db_session, archive_factory, printer_factory):
    """Don't clobber an already-terminal archive (race with on_print_complete).

    The helper docstring says: "only if the archive is still in 'printing',
    so we don't clobber an on_print_complete transition that raced with us."
    This test pins that invariant.
    """
    printer = await printer_factory()
    archive = await archive_factory(printer.id, status="completed", error_message=None)
    archive_id = archive.id

    with patch("backend.app.services.background_dispatch.async_session") as mock_session_factory:
        mock_session_factory.return_value.__aenter__.return_value = db_session
        await BackgroundDispatchService._mark_dispatch_archive_terminal(
            archive_id, "cancelled", "Cancelled before start"
        )

    await db_session.refresh(archive)
    # Race-protection: terminal state was 'completed', not 'printing', so the
    # helper should have left it alone.
    assert archive.status == "completed"
    assert archive.error_message is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_cancel_item_marks_cancelled_and_pauses_queue(db_session, printer_factory):
    """``_cancel_item`` produces the right side-effects for user-initiated cancel.

    Differs from ``_fail_item`` (``failed`` + ``error``) — cancel is
    intentional so the queue pauses for operator review instead of
    flipping into the error state.
    """
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.services.print_scheduler import scheduler

    printer = await printer_factory()
    queue = (
        await db_session.execute(
            __import__("sqlalchemy").select(PrinterQueue).where(PrinterQueue.printer_id == printer.id)
        )
    ).scalar_one_or_none()
    if queue is None:
        # printer_factory may not auto-create a queue row in every test setup;
        # mirror the production behaviour and create one here.
        queue = PrinterQueue(printer_id=printer.id, status="printing")
        db_session.add(queue)
        await db_session.commit()
        await db_session.refresh(queue)
    else:
        queue.status = "printing"
        await db_session.commit()

    item = PrintQueueItem(
        queue_id=queue.id,
        library_file_id=None,
        archive_id=None,
        status="printing",
        position=0,
    )
    db_session.add(item)
    await db_session.commit()
    await db_session.refresh(item)

    await scheduler._cancel_item(db_session, item)

    await db_session.refresh(item)
    await db_session.refresh(queue)

    assert item.status == "cancelled"
    assert item.error_message == "Cancelled by user"
    assert item.completed_at is not None
    assert queue.status == "paused"
    assert queue.current_item_id is None


@pytest.mark.asyncio
async def test_dispatch_finalize_branches_on_cancelled_outcome():
    """``_dispatch_and_finalize`` calls ``_cancel_item`` (not ``_fail_item``)
    when the outcome dict has ``cancelled=True``.

    Pins the branch added by the user-cancel fix: cancel must not fire
    the ``on_queue_job_failed`` notification path.
    """
    from backend.app.services.print_scheduler import scheduler

    cancelled_outcome = {"success": False, "error": "Cancelled", "cancelled": True, "archive_id": 1}

    # The function takes a DB session via ``async_session()`` and looks up
    # the PrintQueueItem. We mock the session factory + the item lookup so
    # the only branch that actually runs is the cancel one.
    fake_item = AsyncMock()
    fake_item.queue_id = 7

    fake_db = AsyncMock()
    fake_db.get = AsyncMock(return_value=fake_item)

    cancel_mock = AsyncMock()
    fail_mock = AsyncMock()
    poweroff_mock = AsyncMock()
    notif_mock = AsyncMock()

    with (
        patch("backend.app.services.print_scheduler.async_session") as session_factory,
        patch.object(scheduler, "_cancel_item", cancel_mock),
        patch.object(scheduler, "_fail_item", fail_mock),
        patch.object(scheduler, "_power_off_if_needed", poweroff_mock),
        patch.object(scheduler, "_mark_printer_dispatched", lambda *a, **kw: None),
        patch(
            "backend.app.services.background_dispatch.background_dispatch.run_from_queue_item",
            new=AsyncMock(return_value=cancelled_outcome),
        ),
        patch(
            "backend.app.services.print_scheduler.notification_service.on_queue_job_failed",
            new=notif_mock,
        ),
    ):
        session_factory.return_value.__aenter__.return_value = fake_db
        await scheduler._dispatch_and_finalize(
            queue_item_id=42,
            printer_id=1,
            printer_name="P1",
            printer_serial=None,
            dispatch_kind="reprint_archive",
            dispatch_source_id=10,
            dispatch_source_name="x.gcode.3mf",
            options={},
            requested_by_user_id=None,
            project_id=None,
            job_name_short="x",
            swap_events=[],
        )

    # Cancel path: _cancel_item was called, _fail_item was NOT, the
    # "queue job failed" notification was NOT fired.
    cancel_mock.assert_awaited_once()
    fail_mock.assert_not_awaited()
    notif_mock.assert_not_called()
    poweroff_mock.assert_awaited_once()
