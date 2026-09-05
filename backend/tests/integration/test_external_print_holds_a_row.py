"""A print BamDude did not send still occupies the printer, so it holds a row.

Not a bug fix — an external print already claims the queue at ``on_print_start``
and there is no dispatch window to protect, because we never dispatched. It is
consistency: after this, "which item is this printer running" has one answer for
all three ways a print can start, and the reprint work that follows can rely on
it.

⚠️ The discriminator is absence, not a new column. A print BamDude dispatched
arrives here with a row already — the scheduler's, or the claim a direct print
took for itself — so "no printing item on this queue" is what external means.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from backend.app.main import mark_queue_printing_for_printer
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue


@pytest.fixture
def main_db(monkeypatch, db_session):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _session_ctx():
        yield db_session

    monkeypatch.setattr("backend.app.main.async_session", _session_ctx)


async def _queue(db_session, printer_factory):
    printer = await printer_factory()
    queue = PrinterQueue(id=printer.id, printer_id=printer.id)
    db_session.add(queue)
    await db_session.commit()
    return printer, queue


async def _printing_rows(db_session, queue_id):
    return (
        (
            await db_session.execute(
                select(PrintQueueItem)
                .where(PrintQueueItem.queue_id == queue_id)
                .where(PrintQueueItem.status == "printing")
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_external_print_gets_a_row(db_session, printer_factory, main_db):
    printer, queue = await _queue(db_session, printer_factory)

    await mark_queue_printing_for_printer(printer.id)

    rows = await _printing_rows(db_session, queue.id)
    assert len(rows) == 1
    await db_session.refresh(queue)
    assert queue.status == "printing"
    assert queue.current_item_id == rows[0].id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_row_says_it_is_external(db_session, printer_factory, main_db):
    """⚠️ **The row exists, so "there is a queue row" cannot mean "queued".**

    That is what this file created, and what the queue-completed notifications
    were still reading as a queue when they fired — an externally-started print
    announced "queue finished — all jobs done" to a farm that had scheduled
    nothing. ``origin`` is the discriminator the file's own header used to say
    was unnecessary ("absence, not a new column"): absence still identifies an
    external print at print START, but by completion the row is present and
    absence has nothing left to say.
    """
    printer, queue = await _queue(db_session, printer_factory)

    await mark_queue_printing_for_printer(printer.id)

    rows = await _printing_rows(db_session, queue.id)
    assert [r.origin for r in rows] == ["external"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_adopted_row_keeps_the_origin_of_whoever_made_it(db_session, printer_factory, main_db):
    """A dispatch of ours reaches ``on_print_start`` too, and adopts its own
    claim rather than making a second row. Adoption must not relabel it: the
    row was made by the Print dialog and stays ``direct``."""
    from backend.app.services.queue_batch import claim_printer_for_direct_print

    printer, queue = await _queue(db_session, printer_factory)
    await claim_printer_for_direct_print(db_session, printer_id=printer.id, origin="direct")

    await mark_queue_printing_for_printer(printer.id)

    rows = await _printing_rows(db_session, queue.id)
    assert [r.origin for r in rows] == ["direct"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_row_carries_the_archive_so_completion_can_close_it(
    db_session, printer_factory, main_db, archive_factory
):
    """``on_print_complete`` auto-deletes a completed item only when it has an
    archive — without this the external row would outlive its print."""
    printer, queue = await _queue(db_session, printer_factory)
    archive = await archive_factory(printer.id, status="printing")

    await mark_queue_printing_for_printer(printer.id, archive_id=archive.id)

    rows = await _printing_rows(db_session, queue.id)
    assert len(rows) == 1
    assert rows[0].archive_id == archive.id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_direct_prints_own_row_is_adopted_not_duplicated(db_session, printer_factory, main_db):
    """⚠️ ``on_print_start`` runs for our own dispatches too, and a second row
    there would trip on_print_complete's "Multiple queue items in 'printing'
    status" guard and attribute the print to whichever came back first."""
    from backend.app.services.queue_batch import claim_printer_for_direct_print

    printer, queue = await _queue(db_session, printer_factory)
    mine = await claim_printer_for_direct_print(db_session, printer_id=printer.id, origin="direct")

    await mark_queue_printing_for_printer(printer.id)

    rows = await _printing_rows(db_session, queue.id)
    assert [r.id for r in rows] == [mine.id]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_adopted_row_learns_its_archive(db_session, printer_factory, main_db, archive_factory):
    """A direct print's row is created before its archive exists — the dispatcher
    wires it, but a re-trigger path that adopts a different archive would leave
    the row pointing nowhere, and a completed item with no archive is never
    auto-deleted."""
    from backend.app.services.queue_batch import claim_printer_for_direct_print

    printer, queue = await _queue(db_session, printer_factory)
    mine = await claim_printer_for_direct_print(db_session, printer_id=printer.id, origin="direct")
    archive = await archive_factory(printer.id, status="printing")

    await mark_queue_printing_for_printer(printer.id, archive_id=archive.id)

    await db_session.refresh(mine)
    assert mine.archive_id == archive.id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_scheduler_item_is_adopted_not_duplicated(db_session, printer_factory, main_db):
    printer, queue = await _queue(db_session, printer_factory)
    item = PrintQueueItem(queue_id=queue.id, status="printing", position=1, started_at=datetime.now(timezone.utc))
    db_session.add(item)
    await db_session.commit()

    await mark_queue_printing_for_printer(printer.id, item.id)

    rows = await _printing_rows(db_session, queue.id)
    assert len(rows) == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_printer_with_no_queue_row_gets_one_and_still_holds_a_row(db_session, printer_factory, main_db):
    """⚠️ Used to be a no-op, and the no-op is what the reporter hit: no queue
    row meant no claim, no claim meant nothing for the completion to close,
    and the plate gate armed regardless — "Repeat" on the card, 409 from the
    route (2026-09-04). A missing queue is created on the spot; the printer's
    id is the queue's id."""
    printer = await printer_factory()

    await mark_queue_printing_for_printer(printer.id)

    queue = (await db_session.execute(select(PrinterQueue).where(PrinterQueue.printer_id == printer.id))).scalar_one()
    assert queue.id == printer.id
    rows = (await db_session.execute(select(PrintQueueItem))).scalars().all()
    assert [(r.queue_id, r.status) for r in rows] == [(queue.id, "printing")]


class TestTheRowSurvivesItsOwnCompletion:
    """⚠️ A new interaction: before this change an external print had no row, so
    ``_completion_belongs_to_item`` never saw one.

    That guard refuses a completion whose subtask name disagrees with the
    archive the row points at — and a refused row is never closed by anything
    else. Its own docstring names the cost: ``check_queue`` counts a printing
    row as a busy printer, so the queue stops until somebody cancels by hand.
    An external print's row must therefore be recognised by its own completion.
    """

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_completion_recognises_the_row_it_created(
        self, db_session, printer_factory, main_db, archive_factory
    ):
        from backend.app.main import _completion_belongs_to_item

        printer, queue = await _queue(db_session, printer_factory)
        # The archive on_print_start builds for a print it did not dispatch:
        # print_name comes from the same subtask_name the completion carries.
        archive = await archive_factory(printer.id, status="printing", print_name="Bracket v3")

        await mark_queue_printing_for_printer(printer.id, archive_id=archive.id)
        row = (await _printing_rows(db_session, queue.id))[0]

        assert await _completion_belongs_to_item(db_session, row, {"subtask_name": "Bracket v3"}) is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_somebody_elses_completion_still_does_not_close_it(
        self, db_session, printer_factory, main_db, archive_factory
    ):
        """The other half — giving external prints a row must not weaken the
        guard. A calibration run or the tail of a previous job arrives on the
        same printer with a different name."""
        from backend.app.main import _completion_belongs_to_item

        printer, queue = await _queue(db_session, printer_factory)
        archive = await archive_factory(printer.id, status="printing", print_name="Bracket v3")

        await mark_queue_printing_for_printer(printer.id, archive_id=archive.id)
        row = (await _printing_rows(db_session, queue.id))[0]

        assert await _completion_belongs_to_item(db_session, row, {"subtask_name": "Something else"}) is False


class TestTheRowCarriesWhatThePrinterToldUs:
    """⚠️ Reported from a farm: repeating a print picked up from BambuStudio went
    out with no AMS mapping.

    The row was created with no options at all, so every print-option column took
    its default — and the two the printer DOES tell us about were thrown away.
    The mapping matters most: without it the reprint is either recomputed from
    whatever is loaded now, or silently downgraded to the external spool.

    ⚠️ Only what the printer actually reports is filled. The calibration flags,
    the macros, the preheat override and gcode injection are parameters of a
    ``project_file`` command BamDude never sent, and the printer does not report
    them back — those stay at their defaults, and no guess is made.
    """

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_slicers_slot_mapping_is_kept(self, db_session, printer_factory, main_db):
        printer, queue = await _queue(db_session, printer_factory)

        await mark_queue_printing_for_printer(
            printer.id, archive_id=None, options={"ams_mapping": [2, -1], "plate_id": 3}
        )

        row = (await _printing_rows(db_session, queue.id))[0]
        assert row.ams_mapping == "[2, -1]"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_plate_is_kept(self, db_session, printer_factory, main_db):
        """A multi-plate file repeated without this prints plate 1."""
        printer, queue = await _queue(db_session, printer_factory)

        await mark_queue_printing_for_printer(printer.id, archive_id=None, options={"plate_id": 3})

        row = (await _printing_rows(db_session, queue.id))[0]
        assert row.plate_id == 3

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_no_options_still_works(self, db_session, printer_factory, main_db):
        """A printer that reported neither must still get its row."""
        printer, queue = await _queue(db_session, printer_factory)

        await mark_queue_printing_for_printer(printer.id)

        row = (await _printing_rows(db_session, queue.id))[0]
        assert row.ams_mapping is None
        assert row.plate_id is None


class TestOnlyWhatThePrinterSaid:
    """⚠️ The guard on the helper: two keys, never a third.

    Everything else a print carries is a parameter of a ``project_file`` command
    BamDude never sent. Inventing a plausible default for one of those would put
    a value the operator never chose onto a row that gets dispatched for real —
    and it would look exactly like a value they did choose.
    """

    def test_it_reports_the_mapping_and_the_plate(self):
        from backend.app.main import _printer_reported_options

        assert _printer_reported_options({"ams_mapping": [2, -1]}, None, 3) == {
            "ams_mapping": [2, -1],
            "plate_id": 3,
        }

    def test_it_invents_nothing_else(self):
        from backend.app.main import _printer_reported_options

        assert set(_printer_reported_options({}, None, None)) == {"ams_mapping", "plate_id"}

    def test_a_silent_printer_yields_no_values(self):
        """Absent is absent — ``_item_columns`` then leaves both columns NULL."""
        from backend.app.main import _printer_reported_options

        assert _printer_reported_options({}, None, None) == {"ams_mapping": None, "plate_id": None}
