"""A refused direct print leaves an honest row: failed, why, and a Retry.

Spec direct-print-silent-cancel §4.1. The row used to become ``cancelled`` —
which reads as the operator's own act — for a print that BamDude refused to
start and will never start on its own (no future autostart).
"""

from datetime import datetime

import pytest

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.filament_deferred import defer_claim
from backend.app.services.filament_intake import routing_detail

pytestmark = pytest.mark.integration


async def test_a_direct_refusal_leaves_a_failed_row_the_operator_can_retry(
    committing_client, db_session, printer_factory
):
    printer = await printer_factory()
    queue = PrinterQueue(id=printer.id, printer_id=printer.id, status="printing")
    db_session.add(queue)
    await db_session.flush()
    started = datetime(2026, 9, 24, 22, 22)
    item = PrintQueueItem(queue_id=queue.id, status="printing", started_at=started, origin="direct")
    db_session.add(item)
    await db_session.flush()
    queue.current_item_id = item.id
    await db_session.commit()

    assert await defer_claim(db_session, item_id=item.id, started_at=started, reason="feed_settle_timeout", direct=True)
    await db_session.commit()
    await db_session.refresh(item)
    await db_session.refresh(queue)

    assert item.status == "failed"
    assert item.error_message == routing_detail("feed_settle_timeout")["message"]
    assert item.waiting_reason is None
    assert item.gate_acknowledged is True, "a print never sent is no physical failure for require_previous_success"
    assert item.completed_at is not None
    assert (queue.status, queue.current_item_id) == ("idle", None)

    retried = await committing_client.post(f"/api/v1/queue/{item.id}/retry")
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "pending"


async def _claimed(db_session, printer_factory, **item):
    printer = await printer_factory()
    queue = PrinterQueue(id=printer.id, printer_id=printer.id, status="printing")
    db_session.add(queue)
    await db_session.flush()
    started = datetime(2026, 11, 14, 19, 0)
    row = PrintQueueItem(queue_id=queue.id, status="printing", started_at=started, origin="direct", position=0, **item)
    db_session.add(row)
    await db_session.flush()
    queue.current_item_id = row.id
    await db_session.commit()
    return queue, row, started


async def test_a_direct_print_onto_an_uncleared_plate_waits_first_in_the_queue(db_session, printer_factory):
    """Owner, 2026-11-18: the one refusal a direct print outlives. The operator asked for this
    print NEXT on a printer whose plate is still occupied; failing it made them retry it, move
    it to the top and clear the plate by hand -- and clearing the plate first let the queue
    start its own next job instead."""
    queue, item, started = await _claimed(db_session, printer_factory)
    # Positions of waiting jobs start at 0 after any «Run next», so 0 alone is not "first".
    behind = PrintQueueItem(queue_id=queue.id, status="pending", position=0)
    later = PrintQueueItem(queue_id=queue.id, status="pending", position=1)
    db_session.add_all([behind, later])
    await db_session.commit()

    result = await defer_claim(
        db_session, item_id=item.id, started_at=started, reason="plate_manual_inspection", direct=True
    )
    await db_session.commit()
    await db_session.refresh(item)
    await db_session.refresh(queue)

    assert result and result.terminal is False
    assert item.status == "pending" and item.error_message is None and item.completed_at is None
    assert (item.waiting_reason, item.waiting_reason_code) == ("Plate not cleared", "plate_not_cleared")
    assert item.started_at is None
    await db_session.refresh(behind)
    await db_session.refresh(later)
    assert item.position < behind.position < later.position, "ahead of every job that was already waiting"
    assert item.origin == "queue", "it is a queued job now, and reported as one when it completes"
    assert (queue.status, queue.current_item_id) == ("idle", None)


async def test_an_uncleared_plate_after_the_command_went_out_still_ends_the_attempt(db_session, printer_factory):
    _queue, item, started = await _claimed(db_session, printer_factory)

    result = await defer_claim(
        db_session,
        item_id=item.id,
        started_at=started,
        reason="plate_manual_inspection",
        direct=True,
        published=True,
    )
    await db_session.commit()
    await db_session.refresh(item)

    assert result.terminal is True and item.status == "failed"


def test_both_runners_ask_about_the_plate_before_they_upload():
    """The refusal used to come after a 33-second upload of a file the printer was never going to print."""
    import inspect

    from backend.app.services.background_dispatch import BackgroundDispatchService as Service

    for runner in (Service._run_reprint_archive, Service._run_print_library_file):
        source = inspect.getsource(runner)
        upload = source.index('"Preparing upload to')
        assert "await plate_admission(admission_db, job, printer)" in source[:upload], runner.__name__
