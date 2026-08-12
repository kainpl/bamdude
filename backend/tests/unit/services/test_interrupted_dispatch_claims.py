"""A dispatch that died between the claim and the archive must not claim the printer for ever.

``_start_print`` commits the claim — item ``printing`` plus
``PrinterQueue.status='printing'`` — and only then spawns the FTP pipeline. The
archive is created inside the dispatcher *before* the upload begins, which is
what makes this recoverable: if the process dies in the window between those two
points, nothing was ever sent to the printer, and the absence of an archive
proves it.

Nothing used to reclaim that. Every path that releases the claim needs either an
archive row (``print_reconciliation`` selects ``PrintArchive.status=='printing'``)
or a live MQTT completion event (the ``on_print_*`` handlers) — and an
interrupted dispatch has neither. ``check_queue`` seeds ``busy_printers`` from a
bare ``SELECT ... WHERE status='printing'`` with no age check and no cross-check
against the printer, so the claim is indistinguishable from a live one and the
printer silently stops taking work. m120's own docstring names this outcome:
"claims the printer forever and the farm quietly stops taking work".

The guards matter more than the fix. m120 refused to repair such rows because "a
stale printing row and a live one are the same row" — so the sweep here only
touches claims it can *prove* are dead, and the two negative tests below are the
proof obligation.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.print_reconciliation import release_interrupted_dispatch_claims


async def _claimed_queue(db_session, printer_id, *, with_item=True, item_status="printing"):
    """A queue holding the dispatch claim, optionally with the item that took it."""
    queue = PrinterQueue(printer_id=printer_id, status="printing", last_activity_at=datetime.now(timezone.utc))
    db_session.add(queue)
    await db_session.flush()

    item = None
    if with_item:
        item = PrintQueueItem(queue_id=queue.id, status=item_status, started_at=datetime.now(timezone.utc))
        db_session.add(item)
        await db_session.flush()
        queue.current_item_id = item.id
    await db_session.commit()
    return queue, item


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_claim_with_no_archive_behind_it_is_released(db_session, printer_factory):
    """The bug: killed between the claim and the archive, the printer stayed claimed.

    No archive exists for this printer, so the dispatcher never got as far as
    creating one — nothing reached the printer and the item can go back in line.
    """
    printer = await printer_factory()
    queue, item = await _claimed_queue(db_session, printer.id)

    released = await release_interrupted_dispatch_claims(db_session)

    await db_session.refresh(queue)
    await db_session.refresh(item)
    assert released == 1
    assert queue.status == "idle", "the printer must be dispatchable again"
    assert queue.current_item_id is None
    assert item.status == "pending", "the item never printed, so it is still owed"
    assert item.started_at is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_claim_backed_by_a_printing_archive_is_left_alone(db_session, printer_factory, archive_factory):
    """The dispatcher creates the archive BEFORE the upload, so an archive means
    the print may well be running. Releasing here would double-dispatch onto a
    busy printer — the exact failure the claim exists to prevent."""
    printer = await printer_factory()
    queue, item = await _claimed_queue(db_session, printer.id)
    await archive_factory(printer.id, status="printing")

    released = await release_interrupted_dispatch_claims(db_session)

    await db_session.refresh(queue)
    await db_session.refresh(item)
    assert released == 0
    assert queue.status == "printing"
    assert item.status == "printing"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_external_print_claim_is_never_touched(db_session, printer_factory):
    """A print started from the printer's own screen claims the queue with no
    item row at all (``set_queue_printing(item_id=None)``). Its truth lives in
    MQTT, not in our tables, and this sweep has no evidence about it."""
    printer = await printer_factory()
    queue, _ = await _claimed_queue(db_session, printer.id, with_item=False)

    released = await release_interrupted_dispatch_claims(db_session)

    await db_session.refresh(queue)
    assert released == 0
    assert queue.status == "printing"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_idle_queue_is_not_disturbed(db_session, printer_factory):
    printer = await printer_factory()
    queue = PrinterQueue(printer_id=printer.id, status="idle")
    db_session.add(queue)
    await db_session.commit()

    released = await release_interrupted_dispatch_claims(db_session)

    await db_session.refresh(queue)
    assert released == 0
    assert queue.status == "idle"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_only_the_printer_with_no_archive_is_released(db_session, printer_factory, archive_factory):
    """Two printers, one interrupted and one genuinely printing: the sweep is
    per-printer, so a live print elsewhere must not shield a dead claim."""
    live_printer = await printer_factory()
    dead_printer = await printer_factory()
    live_queue, live_item = await _claimed_queue(db_session, live_printer.id)
    dead_queue, dead_item = await _claimed_queue(db_session, dead_printer.id)
    await archive_factory(live_printer.id, status="printing")

    released = await release_interrupted_dispatch_claims(db_session)

    await db_session.refresh(live_queue)
    await db_session.refresh(dead_queue)
    assert released == 1
    assert live_queue.status == "printing"
    assert dead_queue.status == "idle"

    remaining = (
        (await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.status == "printing"))).scalars().all()
    )
    assert [i.id for i in remaining] == [live_item.id]
