"""A finished print waits for the operator when the plate must be confirmed.

The row used to be deleted the instant the print completed, so by the time the
operator saw "Clear plate" the job it referred to no longer existed. Repeat needs
something to re-arm, so the row now waits — but only where a confirmation is
actually asked for. Printers with the gate off, and swap printers (which clear
their own plate), must keep vanishing the row exactly as before, or every farm
that never confirms a plate would start accumulating rows nobody answers.
"""

import pytest

# ⚠️ Side effect, not the name: Printer declares its PrinterLocation
# relationship by string and SQLAlchemy cannot resolve it unless this module has
# been imported. Without it the file passes only when another test imported it first.
import backend.app.models.printer_location  # noqa: F401
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.plate_hold import should_hold_for_plate_clear, waiting_row

# ⚠️ No explicit asyncio mark: ``asyncio_mode = "auto"`` in pyproject already
# runs the coroutines here, and marking the module would tag the synchronous
# structural guard at the bottom as async too.
pytestmark = pytest.mark.integration


async def _queue(db_session, printer_factory, **kwargs):
    printer = await printer_factory(**kwargs)
    queue = PrinterQueue(id=printer.id, printer_id=printer.id)
    db_session.add(queue)
    await db_session.commit()
    return printer, queue


async def test_a_printer_that_confirms_the_plate_holds_the_row(db_session, printer_factory):
    printer, _ = await _queue(db_session, printer_factory, require_plate_clear=True)
    assert await should_hold_for_plate_clear(db_session, printer.id, plate_auto_cleared=False) is True


async def test_a_printer_that_does_not_confirm_keeps_todays_behaviour(db_session, printer_factory):
    printer, _ = await _queue(db_session, printer_factory, require_plate_clear=False)
    assert await should_hold_for_plate_clear(db_session, printer.id, plate_auto_cleared=False) is False


async def test_a_swap_that_cleared_the_plate_holds_nothing(db_session, printer_factory):
    """⚠️ The swap path physically cleared the bed, so there is nothing to
    confirm and nobody to ask. Holding here would strand a row on a farm that
    never shows the button."""
    printer, _ = await _queue(db_session, printer_factory, require_plate_clear=True)
    assert await should_hold_for_plate_clear(db_session, printer.id, plate_auto_cleared=True) is False


async def test_an_unknown_printer_holds_nothing(db_session, printer_factory):
    assert await should_hold_for_plate_clear(db_session, 999_999, plate_auto_cleared=False) is False


async def test_waiting_row_finds_the_completed_one(db_session, printer_factory):
    printer, queue = await _queue(db_session, printer_factory)
    db_session.add(PrintQueueItem(queue_id=queue.id, status="pending", position=1))
    done = PrintQueueItem(queue_id=queue.id, status="completed", position=0, archive_id=None)
    db_session.add(done)
    await db_session.commit()

    found = await waiting_row(db_session, printer.id)

    assert found is not None and found.id == done.id


async def test_waiting_row_is_none_when_nothing_finished(db_session, printer_factory):
    printer, queue = await _queue(db_session, printer_factory)
    db_session.add(PrintQueueItem(queue_id=queue.id, status="pending", position=1))
    await db_session.commit()

    assert await waiting_row(db_session, printer.id) is None


async def test_the_completion_handler_keeps_the_row_when_it_will_be_asked_about(db_session, printer_factory):
    """The whole point: the row must still be there when the button is pressed.

    ⚠️ The row is created ``completed`` because that is the order in production —
    ``on_print_complete`` writes ``queue_item.status = queue_status`` well before
    it reaches the cleanup. That ordering is what makes the held row findable:
    ``waiting_row`` looks for ``completed``, so a cleanup moved above the status
    write would hold a row nothing can ever find again.
    """
    import backend.app.main as main_module

    printer, queue = await _queue(db_session, printer_factory, require_plate_clear=True)
    item = PrintQueueItem(queue_id=queue.id, status="completed", position=0, archive_id=1)
    db_session.add(item)
    await db_session.commit()
    item_id = item.id

    kept = await main_module._auto_clean_completed_item(
        db_session, item, queue_status="completed", plate_auto_cleared=False
    )

    assert kept is False, "the row was deleted instead of being held"
    assert await waiting_row(db_session, printer.id) is not None
    assert (await db_session.get(PrintQueueItem, item_id)) is not None


async def test_the_completion_handler_still_deletes_where_nobody_will_be_asked(db_session, printer_factory):
    import backend.app.main as main_module

    printer, queue = await _queue(db_session, printer_factory, require_plate_clear=False)
    item = PrintQueueItem(queue_id=queue.id, status="printing", position=0, archive_id=1)
    db_session.add(item)
    await db_session.commit()
    item_id = item.id

    deleted = await main_module._auto_clean_completed_item(
        db_session, item, queue_status="completed", plate_auto_cleared=False
    )

    assert deleted is True
    assert (await db_session.get(PrintQueueItem, item_id)) is None


async def test_a_swap_printer_still_has_its_row_deleted(db_session, printer_factory):
    """⚠️ The gate never arms after a swap, so a held row would be unanswerable."""
    import backend.app.main as main_module

    printer, queue = await _queue(db_session, printer_factory, require_plate_clear=True)
    item = PrintQueueItem(queue_id=queue.id, status="printing", position=0, archive_id=1)
    db_session.add(item)
    await db_session.commit()
    item_id = item.id

    deleted = await main_module._auto_clean_completed_item(
        db_session, item, queue_status="completed", plate_auto_cleared=True
    )

    assert deleted is True
    assert (await db_session.get(PrintQueueItem, item_id)) is None


async def test_a_failed_print_is_untouched_as_before(db_session, printer_factory):
    """Failed / cancelled / skipped stay put so they can be retried from the queue."""
    import backend.app.main as main_module

    printer, queue = await _queue(db_session, printer_factory, require_plate_clear=True)
    item = PrintQueueItem(queue_id=queue.id, status="failed", position=0, archive_id=1)
    db_session.add(item)
    await db_session.commit()
    item_id = item.id

    deleted = await main_module._auto_clean_completed_item(
        db_session, item, queue_status="failed", plate_auto_cleared=False
    )

    assert deleted is False
    assert (await db_session.get(PrintQueueItem, item_id)) is not None


async def test_clearing_removes_the_waiting_row(db_session, printer_factory):
    from backend.app.services.plate_hold import answer_by_clearing

    printer, queue = await _queue(db_session, printer_factory, require_plate_clear=True)
    done = PrintQueueItem(queue_id=queue.id, status="completed", position=0, archive_id=1)
    db_session.add(done)
    await db_session.commit()
    done_id = done.id

    assert await answer_by_clearing(db_session, printer.id) == 1
    assert (await db_session.get(PrintQueueItem, done_id)) is None


async def test_clearing_with_nothing_waiting_is_a_noop(db_session, printer_factory):
    """⚠️ Reached constantly: a swap printer, a gate armed by the reconnect
    sweep, or a farm upgrading mid-print all release a gate with no row behind
    it. Raising here would turn Clear plate into an error message."""
    from backend.app.services.plate_hold import answer_by_clearing

    printer, _ = await _queue(db_session, printer_factory)
    assert await answer_by_clearing(db_session, printer.id) == 0


async def test_clearing_leaves_the_pending_queue_alone(db_session, printer_factory):
    from backend.app.services.plate_hold import answer_by_clearing

    printer, queue = await _queue(db_session, printer_factory, require_plate_clear=True)
    nxt = PrintQueueItem(queue_id=queue.id, status="pending", position=1)
    db_session.add_all([nxt, PrintQueueItem(queue_id=queue.id, status="completed", position=0, archive_id=1)])
    await db_session.commit()
    nxt_id = nxt.id

    await answer_by_clearing(db_session, printer.id)

    assert (await db_session.get(PrintQueueItem, nxt_id)) is not None


def test_every_place_that_releases_the_gate_answers_the_row():
    """⚠️ A structural guard, because the failure is invisible.

    ``set_awaiting_plate_clear(..., False)`` appears in a handful of places: the
    HTTP route, the Telegram action, and the swap path in on_print_complete
    (which holds no row). A fourth added without answering the held row leaks a
    row that nothing else will ever remove — and nothing would go red.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[3] / "app"
    allowed = ("answer_by_clearing", "answer_by_repeating", "cleared the plate")
    offenders = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"set_awaiting_plate_clear\([^)]*False\)", text):
            # ⚠️ Both directions: the answer legitimately comes before the
            # release (repeat_print re-arms first, then opens the gate) as well
            # as after it (clear_plate releases, then drops the row).
            window = text[max(0, match.start() - 1200) : match.start() + 1200]
            if any(phrase in window for phrase in allowed):
                continue
            offenders.append(f"{path.relative_to(root).as_posix()}:{text[: match.start()].count(chr(10)) + 1}")
    assert offenders == [], "these release the plate gate without answering the held queue row: " + ", ".join(offenders)
