"""Archived printers must never surface as available for auto-queue dispatch.

Guards the ``archived`` availability filter added across the printer-selection
queries (spec 2026-07-12-archived-printers). Uses the AutoQueue distributor as
the representative site — the highest-risk one, since a stray fan-out to a
retired printer would silently strand a job.
"""

import pytest

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.auto_queue_eligibility import find_eligible_printer


@pytest.mark.asyncio
@pytest.mark.integration
async def test_archived_printer_excluded_from_find_eligible(db_session, printer_factory):
    """An archived printer is not eligible even when active, the right model,
    and its queue is auto-distribute-eligible."""
    p = await printer_factory(name="X1", serial_number="ELIG01", model="X1C", is_active=True)
    db_session.add(PrinterQueue(printer_id=p.id, auto_distribute_eligible=True, is_paused=False))
    await db_session.commit()

    item = AutoQueueItem(target_model="X1C")

    p.archived = True
    await db_session.commit()

    printer, reason = await find_eligible_printer(db_session, item, set())
    assert printer is None
    assert reason is not None and "No active" in reason
