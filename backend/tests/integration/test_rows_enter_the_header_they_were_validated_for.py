"""A row enters a queue only while its header is the one it was validated against (G03 B, A25; TA26 (a) on SQLite).

SQLite gives a deleted printer's id -- and its header's -- to the next printer. A queue add validated against header
H, published after H was deleted and another header took its id, writes no row: the header re-read under the lane
is another incarnation, and the door answers as for a missing queue (400 "Queue not found").
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, update

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.schemas.print_queue import PrintQueueItemCreate
from backend.app.services import queue_add
from backend.app.services.queue_add import add_items_to_printer_queue
from backend.tests.integration.test_filament_routing_dispatch import setup_source
from backend.tests.integration.test_queue_add_captures_once import (  # noqa: F401 -- fixtures
    clean_spool_state,
    reads,
    sessions,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_a_header_reissued_under_the_same_id_takes_no_row(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    source, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    await db_session.commit()
    queue_id = queue.id
    real = queue_add.queue_header_holds

    async def reissued_then_checked(session, q, identity):
        # The printer was deleted and the next one took its id and its header's: another incarnation.
        async with sessions() as other:
            await other.execute(
                update(PrinterQueue).where(PrinterQueue.id == q).values(recovery_identity=str(uuid.uuid4()))
            )
            await other.commit()
        return await real(session, q, identity)

    monkeypatch.setattr(queue_add, "queue_header_holds", reissued_then_checked)
    with pytest.raises(HTTPException) as refused:
        await add_items_to_printer_queue(
            db_session, PrintQueueItemCreate(queue_id=queue_id, library_file_id=source.id, plate_id=15), None
        )
    assert (refused.value.status_code, refused.value.detail) == (400, "Queue not found")
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(PrintQueueItem)) == 0


async def test_the_header_check_holds_for_the_same_incarnation(db_session, printer_factory, sessions):
    from backend.app.services.printer_occupancy import queue_header_holds

    printer = await printer_factory()
    db_session.add(PrinterQueue(id=printer.id, printer_id=printer.id))
    await db_session.commit()
    identity = await db_session.scalar(select(PrinterQueue.recovery_identity).where(PrinterQueue.id == printer.id))
    assert await queue_header_holds(db_session, printer.id, identity) is True
    assert await queue_header_holds(db_session, printer.id, "another") is False
    assert await queue_header_holds(db_session, printer.id + 999, identity) is False
