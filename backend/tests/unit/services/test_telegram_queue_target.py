"""Which queue a job the bot creates actually lands on.

Both bot scenes used to build the queue item with ``queue_id=printer_id`` and a
comment asserting the two are the same. They are not: ``PrinterQueue.id`` is an
autoincrement key and ``printer_id`` is a separate unique column, so they agree
only while every queue was created in printer order and none was ever deleted.

That is true of a farm that has never removed a machine — which is why this
survived — and it is guaranteed by nothing. When it breaks it does so silently
and puts the job on **another printer's** queue.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.telegram_handlers.common import resolve_queue_id


@pytest.fixture
def session_factory(test_engine):
    """``resolve_queue_id`` opens its own session, like every other helper in
    ``telegram_handlers/common.py``. Point that at the test database."""
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    with patch("backend.app.core.database.async_session", maker):
        yield maker


async def _printer(db, name: str) -> Printer:
    printer = Printer(name=name, serial_number=f"SN-{name}", ip_address="127.0.0.1", access_code="12345678")
    db.add(printer)
    await db.flush()
    return printer


@pytest.mark.asyncio
async def test_it_finds_the_queue_that_belongs_to_the_printer(db_session, session_factory):
    printer = await _printer(db_session, "alpha")
    queue = PrinterQueue(printer_id=printer.id)
    db_session.add(queue)
    await db_session.commit()

    assert await resolve_queue_id(printer.id) == queue.id


@pytest.mark.asyncio
async def test_it_is_right_even_where_the_ids_have_drifted_apart(db_session, session_factory):
    """⚠️ The case the old assumption got wrong.

    Two printers, and the queue rows created in the OTHER order — which is what
    a deleted-and-recreated printer leaves behind. Reading printer_id as the
    queue id here hands the job to the wrong machine.
    """
    first = await _printer(db_session, "first")
    second = await _printer(db_session, "second")

    later_queue = PrinterQueue(printer_id=second.id)
    db_session.add(later_queue)
    await db_session.flush()
    earlier_queue = PrinterQueue(printer_id=first.id)
    db_session.add(earlier_queue)
    await db_session.commit()

    assert later_queue.id < earlier_queue.id, "fixture no longer reproduces the drift"
    assert await resolve_queue_id(first.id) == earlier_queue.id
    assert await resolve_queue_id(second.id) == later_queue.id


@pytest.mark.asyncio
async def test_a_printer_with_no_queue_answers_none_rather_than_guessing(db_session, session_factory):
    """The caller turns this into a refusal the operator can see. Inventing an
    id would create the item against a queue that does not exist."""
    printer = await _printer(db_session, "queueless")
    await db_session.commit()

    assert await resolve_queue_id(printer.id) is None


def test_the_bot_scenes_no_longer_carry_the_assumption():
    """A source check, because the assumption is one line and reads as
    harmless. Both scenes previously wrote ``queue_id=printer_id`` with a
    comment saying the two are equal."""
    handlers = Path(__file__).resolve().parents[3] / "app" / "services" / "telegram_handlers"

    for name in ("library_scene.py", "queue_scene.py"):
        source = (handlers / name).read_text(encoding="utf-8")
        assert "queue_id=printer_id" not in source, name
        assert "queue_id=queue_id" in source, name


@pytest.mark.asyncio
async def test_the_position_is_the_back_of_that_queue_only(db_session, session_factory):
    """Both scenes used to take max(position) across the WHOLE table.

    The item still went to the back — the value is monotonic — but the number
    reported to the operator was the size of every queue put together, and did
    not match what the same queue shows in the browser.
    """
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.services.telegram_handlers.common import next_queue_position

    mine = PrinterQueue(printer_id=(await _printer(db_session, "mine")).id)
    other = PrinterQueue(printer_id=(await _printer(db_session, "other")).id)
    db_session.add_all([mine, other])
    await db_session.flush()

    db_session.add(PrintQueueItem(queue_id=mine.id, status="pending", position=2))
    # A crowded neighbour, and a finished item of our own — neither may count.
    db_session.add(PrintQueueItem(queue_id=other.id, status="pending", position=97))
    db_session.add(PrintQueueItem(queue_id=mine.id, status="completed", position=50))
    await db_session.commit()

    assert await next_queue_position(db_session, mine.id) == 3


@pytest.mark.asyncio
async def test_the_first_item_in_an_empty_queue_is_position_one(db_session, session_factory):
    from backend.app.services.telegram_handlers.common import next_queue_position

    queue = PrinterQueue(printer_id=(await _printer(db_session, "empty")).id)
    db_session.add(queue)
    await db_session.commit()

    assert await next_queue_position(db_session, queue.id) == 1
