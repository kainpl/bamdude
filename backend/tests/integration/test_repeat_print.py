"""Repeat: print that again, exactly as it was.

The operator's own words for the goal — the same thing as pressing reprint on
the printer itself. Everything the job carried is kept because the row itself is
re-armed; nothing is copied, so there is no field list to fall behind the model.
One had, and it silently dropped ten print options.
"""

import pytest

# ⚠️ Side effect, not the name: Printer declares its PrinterLocation
# relationship by string and SQLAlchemy cannot resolve it unless this module has
# been imported first.
import backend.app.models.printer_location  # noqa: F401
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.plate_hold import answer_by_repeating

pytestmark = pytest.mark.integration


async def _finished(db_session, printer_factory):
    printer = await printer_factory(require_plate_clear=True)
    queue = PrinterQueue(id=printer.id, printer_id=printer.id)
    db_session.add(queue)
    await db_session.flush()
    row = PrintQueueItem(
        queue_id=queue.id,
        status="completed",
        position=0,
        archive_id=7,
        dispatch_attempts=3,
        error_message="something old",
        waiting_reason="something older",
        plate_id=2,
        preheat_override="on",
    )
    db_session.add(row)
    await db_session.commit()
    return printer, queue, row


async def test_the_row_goes_back_to_pending(db_session, printer_factory):
    printer, _, row = await _finished(db_session, printer_factory)

    again = await answer_by_repeating(db_session, printer.id)

    assert again is not None and again.id == row.id, "it must be the SAME row, not a copy"
    assert again.status == "pending"


async def test_it_keeps_every_print_option(db_session, printer_factory):
    """The reason this re-arms instead of cloning."""
    printer, _, _ = await _finished(db_session, printer_factory)

    again = await answer_by_repeating(db_session, printer.id)

    assert again.plate_id == 2
    assert again.preheat_override == "on"


async def test_the_previous_run_is_let_go(db_session, printer_factory):
    """⚠️ The archive belongs to the print that finished. Leaving it wired would
    point a not-yet-started row at a completed print."""
    printer, _, _ = await _finished(db_session, printer_factory)

    again = await answer_by_repeating(db_session, printer.id)

    assert again.archive_id is None
    assert again.completed_at is None
    assert again.started_at is None
    assert again.error_message is None
    assert again.waiting_reason is None


async def test_the_retry_budget_is_reset(db_session, printer_factory):
    """⚠️ Without this a series of repeats exhausts the dispatch cap and the row
    fails "after N attempts" although every one of them succeeded."""
    printer, _, _ = await _finished(db_session, printer_factory)

    again = await answer_by_repeating(db_session, printer.id)

    assert again.dispatch_attempts == 0


async def test_it_goes_to_the_front(db_session, printer_factory):
    """ "Print this again" means now, not after everything already waiting."""
    printer, queue, _ = await _finished(db_session, printer_factory)
    later = PrintQueueItem(queue_id=queue.id, status="pending", position=1)
    db_session.add(later)
    await db_session.commit()

    again = await answer_by_repeating(db_session, printer.id)
    await db_session.refresh(later)

    assert again.position < later.position


async def test_nothing_waiting_returns_none(db_session, printer_factory):
    printer = await printer_factory()
    db_session.add(PrinterQueue(id=printer.id, printer_id=printer.id))
    await db_session.commit()

    assert await answer_by_repeating(db_session, printer.id) is None


async def test_a_second_repeat_works(db_session, printer_factory):
    """The reset has to be complete enough to do twice — the operator will."""
    printer, _, row = await _finished(db_session, printer_factory)
    await answer_by_repeating(db_session, printer.id)

    row.status = "completed"
    row.archive_id = 9
    row.dispatch_attempts = 2
    await db_session.commit()

    again = await answer_by_repeating(db_session, printer.id)

    assert again.id == row.id
    assert again.status == "pending"
    assert again.dispatch_attempts == 0


async def test_the_route_re_arms_and_releases_the_gate(async_client, db_session, printer_factory, monkeypatch):
    """⚠️ Releasing the gate is not decoration. While it is armed
    ``_is_printer_idle`` is False, so a re-armed row would sit there for ever
    and Repeat would look like it did nothing."""
    from unittest.mock import MagicMock

    printer, _, row = await _finished(db_session, printer_factory)
    released = []
    monkeypatch.setattr(
        "backend.app.api.routes.printers.printer_manager.set_awaiting_plate_clear",
        MagicMock(side_effect=lambda pid, val: released.append((pid, val))),
    )

    resp = await async_client.post(f"/api/v1/printers/{printer.id}/repeat-print")

    assert resp.status_code == 200
    assert resp.json()["item_id"] == row.id
    assert released == [(printer.id, False)]


async def test_the_route_refuses_when_nothing_is_waiting(async_client, db_session, printer_factory):
    printer = await printer_factory()
    db_session.add(PrinterQueue(id=printer.id, printer_id=printer.id))
    await db_session.commit()

    resp = await async_client.post(f"/api/v1/printers/{printer.id}/repeat-print")

    assert resp.status_code == 409


async def test_a_held_success_releases_the_previous_success_gate(db_session, printer_factory):
    """⚠️ Not incidental — the design keeps the row `completed` partly for this.

    ``previous_print_succeeded`` reads print_queue for the newest terminal row,
    and a successful one used to be deleted before it could be seen, so a later
    success did not release the require_previous_success gate. The existing gate
    test builds its rows by hand and so never noticed.
    """
    from datetime import datetime, timedelta, timezone

    from backend.app.services.print_scheduler import PrintScheduler

    printer, queue, done = await _finished(db_session, printer_factory)
    done.completed_at = datetime.now(timezone.utc)
    db_session.add(
        PrintQueueItem(
            queue_id=queue.id,
            status="failed",
            position=0,
            completed_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
    )
    await db_session.commit()

    assert await PrintScheduler().previous_print_succeeded(db_session, printer.id) is True
