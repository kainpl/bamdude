"""The bot's Cancel button cancels a queued job the way the queue card does (G03 E-2 item 3, E3a, D7.2).

It wrote ``cancelled`` from its own earlier read -- over a job the scheduler had just claimed -- left the
row without an end time (on PostgreSQL an undated cancel sorted first and hid every later failure from
the "previous print succeeded" gate) and never recounted the card's counter.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import update

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def queued(db_session, printer_factory):
    printer = await printer_factory(name="Bot P1S")
    queue = PrinterQueue(id=printer.id, printer_id=printer.id, pending_count=99)
    db_session.add(queue)
    await db_session.flush()
    item = PrintQueueItem(queue_id=queue.id, status="pending", position=1)
    db_session.add(item)
    await db_session.commit()
    return queue, item


async def _fresh(db_session, model, row_id):
    return await db_session.get(model, row_id, populate_existing=True)


async def test_the_button_cancels_dates_and_recounts(db_session, queued):
    from backend.app.services.telegram_handlers.queue import cb_queue_cancel

    queue, item = queued
    callback = SimpleNamespace(data=f"queue:cancel:{item.id}", answer=AsyncMock())

    with patch("backend.app.services.telegram_handlers.queue.render_queue", new=AsyncMock()):
        await cb_queue_cancel(callback, tg_chat=None)

    row = await _fresh(db_session, PrintQueueItem, item.id)
    assert row.status == "cancelled"
    assert row.completed_at is not None
    assert (await _fresh(db_session, PrinterQueue, queue.id)).pending_count == 0
    assert callback.answer.await_args.args, "the operator is told the job was cancelled"


async def test_a_job_claimed_since_the_list_was_drawn_is_left_printing(db_session, queued):
    from backend.app.services.telegram_handlers.queue import _cancel_pending_item

    queue, item = queued
    await db_session.execute(
        update(PrintQueueItem)
        .where(PrintQueueItem.id == item.id)
        .values(status="printing")
        .execution_options(synchronize_session=False)
    )
    await db_session.commit()

    assert await _cancel_pending_item(item.id) is False
    row = await _fresh(db_session, PrintQueueItem, item.id)
    assert (row.status, row.completed_at) == ("printing", None)
    assert (await _fresh(db_session, PrinterQueue, queue.id)).pending_count == 99, "nothing was written"
