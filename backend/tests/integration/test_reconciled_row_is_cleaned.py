"""A row the startup sweep closes must be tidied like any other.

⚠️ Reported from a farm, 2026-08-21: a swap printer finished a job, the row went
to ``completed`` — and stayed there for hours.

Two paths finish a queue row and only one of them ever cleaned up:

* ``on_print_complete`` selects rows ``WHERE status == 'printing'``, advances
  them, and then auto-cleans;
* ``print_reconciliation._reconcile_complete_archive`` advances the row too —
  and stops there.

So when the sweep wins the race the row is completed by one path and cleaned by
neither: the live handler finds nothing in ``printing`` and skips its whole
block, auto-clean included. Thirty milliseconds apart in the log that day.

⚠️ Not a rare race. The sweep re-arms on every MQTT client recreation, so a
reconnect landing as a print ends is routine — and during a network outage,
which is when reconnects come in waves, it is close to guaranteed.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

# ⚠️ Side effect, not the name: Printer declares its PrinterLocation
# relationship by string and SQLAlchemy cannot resolve it unless this module has
# been imported.
import backend.app.models.printer_location  # noqa: F401
from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.plate_hold import clean_up_finished_row

pytestmark = pytest.mark.integration


async def _finished(db, printer_factory, **printer_kwargs):
    printer = await printer_factory(**printer_kwargs)
    queue = PrinterQueue(id=printer.id, printer_id=printer.id)
    db.add(queue)
    archive = PrintArchive(
        printer_id=printer.id,
        filename="job.3mf",
        file_path="x/job.3mf",
        file_size=1,
        status="completed",
        started_at=datetime.now(timezone.utc),
    )
    db.add(archive)
    await db.commit()
    await db.refresh(archive)

    row = PrintQueueItem(
        queue_id=queue.id,
        status="completed",
        position=0,
        archive_id=archive.id,
        completed_at=datetime.now(timezone.utc),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return printer, queue, row


async def _rows(db, queue_id):
    return (await db.execute(select(PrintQueueItem).where(PrintQueueItem.queue_id == queue_id))).scalars().all()


class TestTheSharedCleanup:
    async def test_a_finished_row_goes(self, db_session, printer_factory):
        printer, queue, row = await _finished(db_session, printer_factory, require_plate_clear=False)

        assert await clean_up_finished_row(db_session, row, queue_status="completed", plate_auto_cleared=False) is True
        assert await _rows(db_session, queue.id) == []

    async def test_a_plate_confirming_printer_keeps_it(self, db_session, printer_factory):
        """The whole point of the hold — somebody is about to be asked."""
        printer, queue, row = await _finished(db_session, printer_factory, require_plate_clear=True)

        assert await clean_up_finished_row(db_session, row, queue_status="completed", plate_auto_cleared=False) is False
        assert len(await _rows(db_session, queue.id)) == 1

    async def test_a_swap_that_cleared_the_plate_still_tidies(self, db_session, printer_factory):
        """⚠️ The reported case: swap printers must keep vanishing the row.

        The plate is physically clear, so there is nothing to ask and nobody to
        ask — a row held here is a row nobody ever answers.
        """
        printer, queue, row = await _finished(db_session, printer_factory, require_plate_clear=True)

        assert await clean_up_finished_row(db_session, row, queue_status="completed", plate_auto_cleared=True) is True
        assert await _rows(db_session, queue.id) == []

    async def test_a_failed_row_is_left_for_the_operator(self, db_session, printer_factory):
        """Unchanged rule: only ``completed`` is swept, so Retry stays possible."""
        printer, queue, row = await _finished(db_session, printer_factory, require_plate_clear=False)
        row.status = "failed"
        await db_session.commit()

        assert await clean_up_finished_row(db_session, row, queue_status="failed", plate_auto_cleared=False) is False
        assert len(await _rows(db_session, queue.id)) == 1

    async def test_a_row_with_no_archive_is_left_alone(self, db_session, printer_factory):
        """It has nothing to live on through — the counters are archive-backed."""
        printer, queue, row = await _finished(db_session, printer_factory, require_plate_clear=False)
        row.archive_id = None
        await db_session.commit()

        assert await clean_up_finished_row(db_session, row, queue_status="completed", plate_auto_cleared=False) is False
        assert len(await _rows(db_session, queue.id)) == 1


class TestBothFinishersUseIt:
    """⚠️ Structural guard. The bug was not in either path's logic — it was that
    one of them simply did not have this step, and nothing said so.
    """

    def test_the_live_handler_and_the_sweep_both_clean_up(self):
        import inspect

        from backend.app import main
        from backend.app.services import print_reconciliation

        assert "clean_up_finished_row" in inspect.getsource(print_reconciliation), (
            "the startup sweep advances a queue row to completed; it must tidy it too"
        )
        assert "_auto_clean_completed_item" in inspect.getsource(main)
