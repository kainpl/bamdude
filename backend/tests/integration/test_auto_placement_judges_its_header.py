"""An AutoQueue placement lands only in the header its eligibility judged (G03 B, A25; review B1-01).

The eligibility picks a printer; before the placement takes the lane, the printer is deleted through the real
DELETE and a new printer takes its id with a new queue header. The placement compares the header under the lane with
the incarnation the eligibility read -- never one it reads itself later -- so nothing is placed in the new printer's
queue: the router row stays pending. Both doors that share the placement: the automatic tick and *Assign now*. An
unchanged printer still gets the job (the control).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, update

from backend.app.core import database
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import auto_queue_eligibility, auto_queue_scheduler as scheduler_module
from backend.tests.integration.test_auto_queue_scheduler import (  # noqa: F401 - fixtures
    _make_printer_with_queue,
    routing_item,
    scheduler,
)
from backend.tests.integration.test_auto_queue_tick_addresses_its_rows import printers_up, row

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def the_printer_is_replaced_after_eligibility(monkeypatch, owner, async_client, printer_factory, printer_id, identity):
    real = owner.find_eligible_printer
    replaced = []

    async def eligibility_then_replace(db, item, *args, **kwargs):
        result = await real(db, item, *args, **kwargs)
        if result.printer is not None and not replaced:
            assert result.printer.id == printer_id and result.queue_identity == identity
            await db.commit()
            response = await async_client.delete(f"/api/v1/printers/{printer_id}?delete_archives=false")
            assert response.status_code == 200, response.text
            # SQLite would hand the next printer this id by itself; PostgreSQL's sequence never does.
            await printer_factory(model="A1MINI", id=printer_id)
            async with database.async_session() as other:
                header = PrinterQueue(id=printer_id, printer_id=printer_id, status="idle")
                other.add(header)
                await other.commit()
                assert header.recovery_identity != identity
            replaced.append(True)
        return result

    monkeypatch.setattr(owner, "find_eligible_printer", eligibility_then_replace)
    return replaced


async def jobs_in(queue_id: int) -> list[int]:
    async with database.async_session() as db:
        return list(await db.scalars(select(PrintQueueItem.id).where(PrintQueueItem.queue_id == queue_id)))


async def clear_header_identity(db, header_id):
    await db.execute(update(PrinterQueue).where(PrinterQueue.id == header_id).values(recovery_identity=None))


@pytest.mark.parametrize("case", ["replaced", "replaced-null", "unchanged", "unchanged-null"])
async def test_the_tick_places_only_in_the_header_its_eligibility_judged(
    async_client, db_session, scheduler, printer_factory, routing_item, monkeypatch, case
):
    printer, header = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
    if case.endswith("-null"):
        await clear_header_identity(db_session, header.id)
    item = routing_item(target_model="A1MINI", status="pending", position=1)
    db_session.add(item)
    await db_session.commit()
    printer_id, identity, item_id = printer.id, header.recovery_identity, item.id
    replaced = (
        the_printer_is_replaced_after_eligibility(
            monkeypatch, scheduler_module, async_client, printer_factory, printer_id, identity
        )
        if case.startswith("replaced")
        else None
    )
    with printers_up(printer_id):
        await scheduler.tick()
    if replaced is not None:
        assert replaced
    if case != "unchanged":
        assert ((await row(item_id)).status, await jobs_in(printer_id)) == ("pending", [])
    else:
        assert (await row(item_id)).status == "assigned"
        assert len(await jobs_in(printer_id)) == 1


@pytest.mark.parametrize("case", ["replaced", "replaced-null", "unchanged", "unchanged-null"])
async def test_assign_now_places_only_in_the_header_its_eligibility_judged(
    async_client, db_session, printer_factory, routing_item, monkeypatch, case
):
    from backend.app.api.routes import auto_queue as auto_queue_routes

    printer, header = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
    if case.endswith("-null"):
        await clear_header_identity(db_session, header.id)
    item = routing_item(target_model="A1MINI", status="pending", position=1)
    db_session.add(item)
    await db_session.commit()
    printer_id, identity, item_id = printer.id, header.recovery_identity, item.id
    replaced = (
        the_printer_is_replaced_after_eligibility(
            monkeypatch, auto_queue_routes, async_client, printer_factory, printer_id, identity
        )
        if case.startswith("replaced")
        else None
    )
    with printers_up(printer_id):
        response = await async_client.post(f"/api/v1/auto-queue/{item_id}/assign-now")
    if replaced is not None:
        assert replaced
    if case != "unchanged":
        assert response.status_code == 409, response.text
        assert ((await row(item_id)).status, await jobs_in(printer_id)) == ("pending", [])
    else:
        assert response.status_code == 200, response.text
        assert (await row(item_id)).status == "assigned"
        assert len(await jobs_in(printer_id)) == 1


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
def test_the_eligibility_reads_the_header_with_the_printer():
    """The incarnation comes from the read that chose the printers, not from a read of its own afterwards."""
    import inspect

    source = inspect.getsource(auto_queue_eligibility.printers_for_item)
    assert "select(Printer, PrinterQueue.status, PrinterQueue.recovery_identity)" in source
