"""Archived printers' queues must not appear in the queue view (GET /queues/).

Companion to test_archived_printer_eligibility: the archived filter has to reach
the queue-list endpoint too, otherwise a retired printer's queue card lingers in
the Queue page even though the printer itself is gone.
"""

import pytest
from httpx import AsyncClient

from backend.app.models.printer_queue import PrinterQueue


@pytest.mark.asyncio
@pytest.mark.integration
async def test_archived_printer_queue_hidden(async_client: AsyncClient, printer_factory, db_session):
    live = await printer_factory(name="Live", serial_number="QLIVE1")
    retired = await printer_factory(name="Retired", serial_number="QARCH1")
    db_session.add(PrinterQueue(id=live.id, printer_id=live.id))
    db_session.add(PrinterQueue(id=retired.id, printer_id=retired.id))
    await db_session.commit()

    retired.archived = True
    await db_session.commit()

    resp = await async_client.get("/api/v1/queues/")
    assert resp.status_code == 200
    printer_ids = {q["printer_id"] for q in resp.json()}
    assert live.id in printer_ids
    assert retired.id not in printer_ids
