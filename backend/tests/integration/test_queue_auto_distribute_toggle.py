"""The auto-distribute opt-out is settable through the API.

``printer_queues.auto_distribute_eligible`` was read by the auto-queue router
since m024 but had NO write path — the opt-out only worked if somebody edited
the DB by hand (found by the 2026-08-23 dead-code audit; wired on request).
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.models.printer_queue import PrinterQueue


async def _queue(db_session, printer_factory, **kw):
    printer = await printer_factory(**kw)
    queue = PrinterQueue(id=printer.id, printer_id=printer.id)
    db_session.add(queue)
    await db_session.commit()
    return queue


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_opt_out_round_trips(async_client: AsyncClient, printer_factory, db_session):
    queue = await _queue(db_session, printer_factory, serial_number="QADE1")

    resp = await async_client.patch(f"/api/v1/queues/{queue.id}", json={"auto_distribute_eligible": False})
    assert resp.status_code == 200
    assert resp.json()["auto_distribute_eligible"] is False

    row = (await db_session.execute(select(PrinterQueue).where(PrinterQueue.id == queue.id))).scalar_one()
    await db_session.refresh(row)
    assert row.auto_distribute_eligible is False

    resp = await async_client.patch(f"/api/v1/queues/{queue.id}", json={"auto_distribute_eligible": True})
    assert resp.status_code == 200
    assert resp.json()["auto_distribute_eligible"] is True


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_flag_is_visible_in_the_list(async_client: AsyncClient, printer_factory, db_session):
    queue = await _queue(db_session, printer_factory, serial_number="QADE2")
    queue.auto_distribute_eligible = False
    await db_session.commit()

    resp = await async_client.get("/api/v1/queues/")
    assert resp.status_code == 200
    mine = next(q for q in resp.json() if q["id"] == queue.id)
    assert mine["auto_distribute_eligible"] is False


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_patch_without_the_field_leaves_it_alone(async_client: AsyncClient, printer_factory, db_session):
    queue = await _queue(db_session, printer_factory, serial_number="QADE3")
    queue.auto_distribute_eligible = False
    await db_session.commit()

    resp = await async_client.patch(f"/api/v1/queues/{queue.id}", json={"is_paused": True})
    assert resp.status_code == 200
    assert resp.json()["auto_distribute_eligible"] is False
