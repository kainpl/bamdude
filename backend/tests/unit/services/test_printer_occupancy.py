"""Persistent claim facts must survive a stale/missing queue header."""

import pytest

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.auto_queue_eligibility import busy_printer_ids
from backend.app.services.printer_occupancy import (
    PrinterOccupancyConflict,
    read_queue_occupancy,
    require_scheduler_claim,
)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_printing_child_blocks_scheduler_and_auto_router_with_an_idle_header(db_session, printer_factory):
    printer = await printer_factory()
    queue = PrinterQueue(id=printer.id, printer_id=printer.id, status="idle")
    db_session.add_all([queue, PrintQueueItem(queue_id=printer.id, status="printing", position=0)])
    await db_session.commit()

    occupancy = await read_queue_occupancy(db_session, queue.id)
    with pytest.raises(PrinterOccupancyConflict, match="active_claim"):
        require_scheduler_claim(occupancy)
    assert printer.id in await busy_printer_ids(db_session)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_header_only_claim_blocks_scheduler(db_session, printer_factory):
    printer = await printer_factory()
    queue = PrinterQueue(id=printer.id, printer_id=printer.id, status="printing")
    db_session.add(queue)
    await db_session.commit()

    with pytest.raises(PrinterOccupancyConflict, match="active_claim"):
        require_scheduler_claim(await read_queue_occupancy(db_session, queue.id))
