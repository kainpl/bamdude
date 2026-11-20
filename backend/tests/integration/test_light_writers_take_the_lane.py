"""The light writers act under the lanes, on the rows they read (G03 B, A7; acceptance TA8 on SQLite).

Each writer reads its rows (and their queues) in the request session, ends that read, and writes in a fresh session
under the lanes of those queues. A claim — or a move to another queue — that lands between the read and the lanes
is found under them: the writer leaves that row exactly as the claim left it, refuses where it acts on the rows as
one (group, manual start, filing), and writes nothing it was not still entitled to.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime

import pytest
from sqlalchemy import func, select, update

from backend.app.core import database
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import queue_ops

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

CLAIMED = datetime(2026, 11, 20, 12, 0, 0)


async def a_queue(db_session, printer_factory) -> int:
    printer = await printer_factory()
    db_session.add(PrinterQueue(id=printer.id, printer_id=printer.id, status="idle"))
    await db_session.commit()
    return printer.id


async def pending_rows(db_session, queue_id: int, count: int = 1, **row) -> list[int]:
    items = [PrintQueueItem(queue_id=queue_id, status="pending", position=i, **row) for i in range(count)]
    db_session.add_all(items)
    await db_session.commit()
    return [item.id for item in items]


async def row(item_id: int) -> PrintQueueItem:
    async with database.async_session() as db:
        return await db.get(PrintQueueItem, item_id)


def claim(item_id: int):
    async def change():
        async with database.async_session() as db:
            await db.execute(
                update(PrintQueueItem).where(PrintQueueItem.id == item_id).values(status="printing", started_at=CLAIMED)
            )
            await db.commit()

    return change


def move(item_id: int, queue_id: int):
    async def change():
        async with database.async_session() as db:
            await db.execute(update(PrintQueueItem).where(PrintQueueItem.id == item_id).values(queue_id=queue_id))
            await db.commit()

    return change


def before_the_lane(monkeypatch, change, name: str = "lane_session", module=queue_ops):
    """``change`` commits after the writer's read ended and before it enters its lanes."""
    real = getattr(module, name)

    @asynccontextmanager
    async def entered(*args, **kwargs):
        await change()
        async with real(*args, **kwargs) as value:
            yield value

    monkeypatch.setattr(module, name, entered)


# --- skip ---------------------------------------------------------------------------------------------------------


async def test_a_skip_leaves_a_row_claimed_before_the_lane_printing(
    async_client, db_session, printer_factory, monkeypatch
):
    queue_id = await a_queue(db_session, printer_factory)
    (item_id,) = await pending_rows(db_session, queue_id)
    before_the_lane(monkeypatch, claim(item_id))
    response = await async_client.post(f"/api/v1/queue/{item_id}/skip")
    assert response.status_code == 409, response.text
    current = await row(item_id)
    assert (current.status, current.started_at) == ("printing", CLAIMED)


async def test_a_skip_leaves_a_row_moved_to_another_queue_as_it_is(
    async_client, db_session, printer_factory, monkeypatch
):
    queue_id = await a_queue(db_session, printer_factory)
    other = await a_queue(db_session, printer_factory)
    (item_id,) = await pending_rows(db_session, queue_id)
    before_the_lane(monkeypatch, move(item_id, other))
    response = await async_client.post(f"/api/v1/queue/{item_id}/skip")
    assert response.status_code == 409, response.text
    current = await row(item_id)
    assert (current.status, current.queue_id) == ("pending", other)


async def test_a_batch_skip_skips_only_the_copies_still_pending(async_client, db_session, printer_factory, monkeypatch):
    queue_id = await a_queue(db_session, printer_factory)
    first, second = await pending_rows(db_session, queue_id, count=2, batch_id="b-1")
    before_the_lane(monkeypatch, claim(first))
    response = await async_client.post(f"/api/v1/queue/batch/b-1/skip?queue_id={queue_id}")
    assert response.status_code == 200, response.text
    assert response.json()["skipped"] == 1
    assert ((await row(first)).status, (await row(second)).status) == ("printing", "skipped")


# --- group / manual start -----------------------------------------------------------------------------------------


async def test_a_group_with_one_row_claimed_before_the_lane_groups_nothing(
    async_client, db_session, printer_factory, monkeypatch
):
    queue_id = await a_queue(db_session, printer_factory)
    first, second = await pending_rows(db_session, queue_id, count=2)
    before_the_lane(monkeypatch, claim(second))
    response = await async_client.post("/api/v1/queue/batch", json={"item_ids": [first, second]})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"] == "queue_item_changed"
    assert ((await row(first)).batch_id, (await row(second)).batch_id) == (None, None), "all or nothing"
    assert (await row(second)).status == "printing"


async def test_a_group_with_one_row_moved_before_the_lane_groups_nothing(
    async_client, db_session, printer_factory, monkeypatch
):
    queue_id = await a_queue(db_session, printer_factory)
    other = await a_queue(db_session, printer_factory)
    first, second = await pending_rows(db_session, queue_id, count=2)
    before_the_lane(monkeypatch, move(first, other))
    response = await async_client.post("/api/v1/queue/batch", json={"item_ids": [first, second]})
    assert response.status_code == 409, response.text
    assert ((await row(first)).batch_id, (await row(second)).batch_id) == (None, None)
    assert (await row(first)).queue_id == other


async def test_an_untouched_group_is_written(async_client, db_session, printer_factory):
    queue_id = await a_queue(db_session, printer_factory)
    first, second = await pending_rows(db_session, queue_id, count=2)
    response = await async_client.post("/api/v1/queue/batch", json={"item_ids": [first, second]})
    assert response.status_code == 200, response.text
    batch = response.json()["batch_id"]
    assert ((await row(first)).batch_id, (await row(second)).batch_id) == (batch, batch)


async def test_a_manual_start_toggle_of_a_row_claimed_before_the_lane_writes_nothing(
    async_client, db_session, printer_factory, monkeypatch
):
    queue_id = await a_queue(db_session, printer_factory)
    (item_id,) = await pending_rows(db_session, queue_id, manual_start=True)
    before_the_lane(monkeypatch, claim(item_id))
    response = await async_client.patch(f"/api/v1/queue/{item_id}/manual-start")
    assert response.status_code == 409, response.text
    current = await row(item_id)
    assert (current.status, current.manual_start) == ("printing", True)


async def test_start_of_a_row_claimed_before_the_lane_writes_nothing(
    async_client, db_session, printer_factory, monkeypatch
):
    queue_id = await a_queue(db_session, printer_factory)
    (item_id,) = await pending_rows(db_session, queue_id, manual_start=True)
    before_the_lane(monkeypatch, claim(item_id), name="queue_claim_scope")
    response = await async_client.post(f"/api/v1/queue/{item_id}/start")
    assert response.status_code == 400, response.text
    current = await row(item_id)
    assert (current.status, current.manual_start) == ("printing", True)


async def test_start_of_an_untouched_row_clears_its_manual_start(async_client, db_session, printer_factory):
    queue_id = await a_queue(db_session, printer_factory)
    (item_id,) = await pending_rows(db_session, queue_id, manual_start=True)
    response = await async_client.post(f"/api/v1/queue/{item_id}/start")
    assert response.status_code == 200, response.text
    assert (await row(item_id)).manual_start is False


# --- bulk reorder -------------------------------------------------------------------------------------------------


async def test_a_bulk_reorder_leaves_the_claimed_row_where_it_was(
    async_client, db_session, printer_factory, monkeypatch
):
    queue_id = await a_queue(db_session, printer_factory)
    first, second = await pending_rows(db_session, queue_id, count=2)
    before_the_lane(monkeypatch, claim(first))
    response = await async_client.post(
        "/api/v1/queue/reorder",
        json={"items": [{"id": first, "position": 7}, {"id": second, "position": 8}]},
    )
    assert response.status_code == 200, response.text
    assert ((await row(first)).position, (await row(second)).position) == (0, 8)


# --- printer archive ----------------------------------------------------------------------------------------------


async def test_archiving_a_printer_leaves_a_job_claimed_before_the_lane_printing(
    async_client, db_session, printer_factory, monkeypatch
):
    queue_id = await a_queue(db_session, printer_factory)
    first, second = await pending_rows(db_session, queue_id, count=2)
    before_the_lane(monkeypatch, claim(first))
    response = await async_client.post(f"/api/v1/printers/{queue_id}/archive")
    assert response.status_code == 200, response.text
    assert ((await row(first)).status, (await row(second)).status) == ("printing", "cancelled")


async def test_archiving_a_printer_whose_queues_changed_meanwhile_changes_nothing(
    async_client, db_session, printer_factory, monkeypatch
):
    printer = await printer_factory()

    async def a_queue_appears():
        async with database.async_session() as db:
            db.add(PrinterQueue(id=printer.id, printer_id=printer.id, status="idle"))
            await db.commit()

    before_the_lane(monkeypatch, a_queue_appears)
    response = await async_client.post(f"/api/v1/printers/{printer.id}/archive")
    assert response.status_code == 409, response.text
    async with database.async_session() as db:
        assert (await db.get(Printer, printer.id)).archived is False


# --- filing under an order ----------------------------------------------------------------------------------------


async def test_filing_a_job_claimed_before_the_lane_files_nothing(
    committing_client, db_session, printer_factory, monkeypatch
):
    from backend.app.services import order_fulfilment

    project = await committing_client.post("/api/v1/projects/", json={"name": "O"})
    assert project.status_code in (200, 201), project.text
    project_id = project.json()["id"]
    queue_id = await a_queue(db_session, printer_factory)
    first, second = await pending_rows(db_session, queue_id, count=2)

    real = order_fulfilment.lock_orders

    async def claim_then_lock(db, orders):
        await claim(second)()
        return await real(db, orders)

    monkeypatch.setattr(order_fulfilment, "lock_orders", claim_then_lock)
    response = await committing_client.post(
        f"/api/v1/projects/{project_id}/add-queue", json={"queue_item_ids": [first, second]}
    )
    assert response.status_code == 409, response.text
    assert ((await row(first)).project_id, (await row(second)).project_id) == (None, None)


# --- the calibration insert ---------------------------------------------------------------------------------------


async def calibration_insert(monkeypatch, printer_id: int) -> int:
    from backend.app.services import background_dispatch

    monkeypatch.setattr(background_dispatch, "async_session", database.async_session)
    return await background_dispatch.enqueue_calibration_print(
        printer_id=printer_id,
        asset_path="calib.3mf",
        cali_mode="flow",
        user_id=None,
        ams_id=0,
        slot_id=0,
        tray_id=0,
    )


async def test_a_calibration_insert_goes_to_the_head_and_recounts(db_session, printer_factory, monkeypatch):
    queue_id = await a_queue(db_session, printer_factory)
    (waiting,) = await pending_rows(db_session, queue_id)
    item_id = await calibration_insert(monkeypatch, queue_id)
    inserted = await row(item_id)
    assert (inserted.queue_id, inserted.position, inserted.is_calibration) == (queue_id, 0, True)
    assert (await row(waiting)).status == "pending"


async def test_a_calibration_insert_into_a_replaced_header_inserts_nothing(db_session, printer_factory, monkeypatch):
    queue_id = await a_queue(db_session, printer_factory)

    async def header_replaced():
        async with database.async_session() as db:
            await db.execute(
                update(PrinterQueue).where(PrinterQueue.id == queue_id).values(recovery_identity="another-incarnation")
            )
            await db.commit()

    before_the_lane(monkeypatch, header_replaced, name="queue_claim_scope")
    with pytest.raises(ValueError):
        await calibration_insert(monkeypatch, queue_id)
    async with database.async_session() as db:
        assert await db.scalar(select(func.count()).select_from(PrintQueueItem)) == 0
