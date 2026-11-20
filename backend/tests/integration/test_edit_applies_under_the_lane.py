"""An edit validates outside the lane and applies only to the row it validated (G03 B, A6; acceptance TA7 on SQLite).

A claim lands between the edit's validation (``routing_update``, which reads the job's bytes) and its apply: the
single and the batch edit answer 409 ``queue_item_changed`` and write nothing, the bulk edit skips that row; the
row that started printing is left exactly as the claim wrote it.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import update

from backend.app.core import database
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

CLAIMED = datetime(2026, 11, 20, 12, 0, 0)


async def pending_rows(db_session, printer_factory, count=1, **row):
    printer = await printer_factory()
    queue = PrinterQueue(id=printer.id, printer_id=printer.id, status="idle")
    db_session.add(queue)
    await db_session.commit()
    items = [PrintQueueItem(queue_id=queue.id, status="pending", position=i, **row) for i in range(count)]
    db_session.add_all(items)
    await db_session.commit()
    return queue.id, [item.id for item in items]


def a_claim_during_validation(monkeypatch, item_id: int):
    """``routing_update`` (the validation's blob read) is where the request awaits: the claim lands there."""
    from backend.app.api.routes import print_queue

    real = print_queue.routing_update
    fired = []

    async def validation(db, item, changes, *args, **kwargs):
        result = await real(db, item, changes, *args, **kwargs)
        if not fired:
            fired.append(True)
            async with database.async_session() as claim:
                await claim.execute(
                    update(PrintQueueItem)
                    .where(PrintQueueItem.id == item_id)
                    .values(status="printing", started_at=CLAIMED)
                )
                await claim.commit()
        return result

    monkeypatch.setattr(print_queue, "routing_update", validation)


async def row(item_id):
    async with database.async_session() as db:
        return await db.get(PrintQueueItem, item_id)


async def test_a_single_edit_of_a_job_claimed_meanwhile_saves_nothing(
    async_client, db_session, printer_factory, monkeypatch
):
    _queue, (item_id,) = await pending_rows(db_session, printer_factory)
    a_claim_during_validation(monkeypatch, item_id)
    response = await async_client.patch(f"/api/v1/queue/{item_id}", json={"layer_inspect": True})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"] == "queue_item_changed"
    current = await row(item_id)
    assert (current.status, current.started_at, current.layer_inspect) == ("printing", CLAIMED, False)


async def test_an_untouched_single_edit_is_saved(async_client, db_session, printer_factory):
    _queue, (item_id,) = await pending_rows(db_session, printer_factory)
    response = await async_client.patch(f"/api/v1/queue/{item_id}", json={"layer_inspect": True})
    assert response.status_code == 200, response.text
    assert (await row(item_id)).layer_inspect is True


async def test_a_bulk_edit_skips_the_row_claimed_meanwhile_and_applies_the_rest(
    async_client, db_session, printer_factory, monkeypatch
):
    _queue, (first, second) = await pending_rows(db_session, printer_factory, count=2)
    a_claim_during_validation(monkeypatch, first)
    response = await async_client.patch("/api/v1/queue/bulk", json={"item_ids": [first, second], "layer_inspect": True})
    assert response.status_code == 200, response.text
    assert (response.json()["updated_count"], response.json()["skipped_count"]) == (1, 1)
    assert (await row(first)).layer_inspect is False
    assert (await row(second)).layer_inspect is True


async def test_a_batch_edit_with_one_copy_claimed_meanwhile_saves_nothing(
    async_client, db_session, printer_factory, monkeypatch
):
    queue_id, (first, second) = await pending_rows(db_session, printer_factory, count=2, batch_id="b-1")
    a_claim_during_validation(monkeypatch, first)
    response = await async_client.patch(f"/api/v1/queue/batch/b-1?queue_id={queue_id}", json={"layer_inspect": True})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"] == "queue_item_changed"
    assert (await row(first)).layer_inspect is False
    assert (await row(second)).layer_inspect is False, "the batch moves whole or not at all"
