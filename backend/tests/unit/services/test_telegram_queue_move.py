"""The Telegram move is the web reorder's door (G03 B, W10).

The bot moved a job by swapping it with the adjacent pending row by position — whatever queue that row was in, and
under no lane. It now moves the row's block one step within ITS queue through ``queue_ops.reorder_block``, under
that queue's lane, and recounts.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.telegram_handlers import queue as handler


@pytest.fixture
def session_factory(test_engine):
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    with patch("backend.app.core.database.async_session", maker):
        yield maker


async def a_queue(db, name: str) -> int:
    printer = Printer(name=name, serial_number=f"SN-{name}", ip_address="127.0.0.1", access_code="12345678")
    db.add(printer)
    await db.flush()
    db.add(PrinterQueue(id=printer.id, printer_id=printer.id, status="idle"))
    await db.flush()
    return printer.id


@pytest.mark.asyncio
async def test_the_bot_moves_a_job_only_within_its_own_queue(db_session, session_factory, monkeypatch):
    monkeypatch.setattr(handler, "has_perm", lambda chat, perm: True)
    monkeypatch.setattr(handler, "deny_out_of_scope", AsyncMock(return_value=False))
    monkeypatch.setattr(handler, "get_language", AsyncMock(return_value="en"))
    monkeypatch.setattr(handler, "cb_queue_detail", AsyncMock())
    mine, theirs = await a_queue(db_session, "mine"), await a_queue(db_session, "theirs")
    first = PrintQueueItem(queue_id=mine, status="pending", position=0)
    neighbour = PrintQueueItem(queue_id=theirs, status="pending", position=1)
    moving = PrintQueueItem(queue_id=mine, status="pending", position=2)
    db_session.add_all([first, neighbour, moving])
    await db_session.commit()
    ids = first.id, neighbour.id, moving.id

    callback = MagicMock()
    callback.data = f"queue:move:{moving.id}:up"
    callback.answer = AsyncMock()
    await handler.cb_queue_move(callback, None)

    async with session_factory() as db:
        positions = [(await db.get(PrintQueueItem, item_id)).position for item_id in ids]
        queue = await db.get(PrinterQueue, mine)
    assert positions == [2, 1, 0], "swapped with the row of its own queue, the other queue's row untouched"
    assert queue.pending_count == 2
