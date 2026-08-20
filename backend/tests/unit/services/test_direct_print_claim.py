"""A direct print takes the same DB claim a queued one does.

Print now used to claim nothing until the printer reported the job, which is
why the queue could dispatch over it. The row created here is what closes that
window — and ``status='printing'`` is deliberate: it is exactly the state the
scheduler's own dispatch reaches before it uploads, so the dispatch CAS
(gated on ``pending``) can never pick it up.
"""

import pytest

from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.queue_batch import claim_printer_for_direct_print


async def _queue(db_session, printer_factory):
    printer = await printer_factory()
    queue = PrinterQueue(id=printer.id, printer_id=printer.id)
    db_session.add(queue)
    await db_session.commit()
    return printer, queue


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_claim_is_a_printing_row_the_scheduler_cannot_pick_up(db_session, printer_factory):
    printer, queue = await _queue(db_session, printer_factory)

    item = await claim_printer_for_direct_print(
        db_session, printer_id=printer.id, library_file_id=None, created_by_id=None
    )

    assert item is not None
    assert item.status == "printing", "pending would let the scheduler dispatch it a second time"
    assert item.started_at is not None
    assert item.position == 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_queue_is_claimed_and_points_at_the_item(db_session, printer_factory):
    printer, queue = await _queue(db_session, printer_factory)

    item = await claim_printer_for_direct_print(db_session, printer_id=printer.id)

    await db_session.refresh(queue)
    assert queue.status == "printing", "this is the seed check_queue reads"
    assert queue.current_item_id == item.id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_it_does_not_disturb_the_pending_ordering(db_session, printer_factory):
    """⚠️ position 0 and status printing: MAX(position) is taken over pending
    rows only, so the next queued item must still land at 1."""
    printer, queue = await _queue(db_session, printer_factory)
    await claim_printer_for_direct_print(db_session, printer_id=printer.id)

    from backend.app.services.queue_batch import enqueue_batch_copies

    items, _ = await enqueue_batch_copies(db_session, printer_id=printer.id, count=1)

    assert items[0].position == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_print_options_land_on_the_row(db_session, printer_factory):
    """The row is what the queue UI renders and what the dispatcher reads back."""
    printer, queue = await _queue(db_session, printer_factory)

    item = await claim_printer_for_direct_print(
        db_session,
        printer_id=printer.id,
        library_file_id=None,
        options={"plate_id": 3, "ams_mapping": [1, -1], "timelapse": True, "layer_inspect": True},
    )

    assert item.plate_id == 3
    assert item.ams_mapping == "[1, -1]"
    assert item.timelapse is True
    assert item.layer_inspect is True


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_owner_is_carried(db_session, printer_factory):
    """``queue:read_own`` filters on it — an ownerless row is invisible to whoever made it."""
    from backend.app.models.user import User

    printer, queue = await _queue(db_session, printer_factory)
    user = User(username="claimant")
    db_session.add(user)
    await db_session.commit()

    item = await claim_printer_for_direct_print(db_session, printer_id=printer.id, created_by_id=user.id)

    assert item.created_by_id == user.id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_printer_with_no_queue_row_claims_nothing(db_session, printer_factory):
    """⚠️ Returns None rather than raising: the caller must be able to carry on
    dispatching. A printer without a queue row is a broken install, not a
    reason to refuse someone's print."""
    printer = await printer_factory()

    assert await claim_printer_for_direct_print(db_session, printer_id=printer.id) is None
