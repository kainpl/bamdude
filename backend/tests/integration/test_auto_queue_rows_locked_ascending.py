"""Several router rows are locked ascending, as one set, before the first write (G03 B, A20; TA25 on SQLite).

``queue_counters.lock_auto_rows`` records what the transaction holds in its lock ledger: a lower id after a higher
one raises ``AutoRowOrderError``, a held id is not asked for again, a rolled-back savepoint forgets its own. The
AutoQueue edit doors validate outside any lock and write only the rows the validation read: a single PUT on a row
placed meanwhile is 409 ``queue_item_changed``, the batch PUT writes all or none, the reorder leaves a row that
left pending where it was, a retry names the incarnation it revalidated.
"""

from __future__ import annotations

import pytest
from sqlalchemy import update

from backend.app.core import database
from backend.app.core.lock_ledger import ledger
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.services.queue_counters import AutoRowOrderError, lock_auto_rows

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

TABLE = "auto_queue_items"


async def router_rows(db_session, *ids, **values) -> list[int]:
    rows = [AutoQueueItem(id=i, status="pending", position=i, **values) for i in ids]
    db_session.add_all(rows)
    await db_session.commit()
    return [row.id for row in rows]


async def row(item_id: int) -> AutoQueueItem | None:
    async with database.async_session() as db:
        return await db.get(AutoQueueItem, item_id)


async def placed_meanwhile(*item_ids):
    async with database.async_session() as other:
        await other.execute(update(AutoQueueItem).where(AutoQueueItem.id.in_(item_ids)).values(status="assigned"))
        await other.commit()


# --- the helper (TA25 h) --------------------------------------------------------------------------------------------


async def test_a_lower_id_after_a_higher_one_raises_and_locks_nothing_more(db_session):
    await router_rows(db_session, 3, 5, 7)
    await lock_auto_rows(db_session, [7, 5])
    with pytest.raises(AutoRowOrderError):
        await lock_auto_rows(db_session, [3])
    assert ledger(db_session).held(TABLE) == {5, 7}


async def test_a_held_id_is_not_asked_for_again(db_session):
    await router_rows(db_session, 5, 7, 9)
    await lock_auto_rows(db_session, [5, 7])
    rows = await lock_auto_rows(db_session, [5, 9])
    assert set(rows) == {5, 9}
    assert ledger(db_session).held(TABLE) == {5, 7, 9}


async def test_a_rolled_back_savepoint_forgets_its_own_locks(db_session):
    await router_rows(db_session, 5, 7, 8)
    await lock_auto_rows(db_session, [5, 7])
    savepoint = await db_session.begin_nested()
    await lock_auto_rows(db_session, [8])
    assert ledger(db_session).held(TABLE) == {5, 7, 8}
    await savepoint.rollback()
    assert ledger(db_session).held(TABLE) == {5, 7}


async def test_a_new_transaction_starts_with_nothing_held(db_session):
    await router_rows(db_session, 5, 7)
    await lock_auto_rows(db_session, [7])
    await db_session.commit()
    await lock_auto_rows(db_session, [5])
    assert ledger(db_session).held(TABLE) == {5}


# --- the edit doors -------------------------------------------------------------------------------------------------


def before_the_apply(monkeypatch, change):
    from backend.app.api.routes import auto_queue as route

    real = route.begin_router_write

    async def entered(db):
        await change()
        return await real(db)

    monkeypatch.setattr(route, "begin_router_write", entered)


async def test_a_single_edit_of_a_row_placed_meanwhile_saves_nothing(async_client, db_session, monkeypatch):
    (item_id,) = await router_rows(db_session, 11)
    before_the_apply(monkeypatch, lambda: placed_meanwhile(item_id))
    response = await async_client.put(f"/api/v1/auto-queue/{item_id}", json={"layer_inspect": True})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"] == "queue_item_changed"
    current = await row(item_id)
    assert (current.status, current.layer_inspect) == ("assigned", False)


async def test_an_untouched_single_edit_is_saved(async_client, db_session):
    (item_id,) = await router_rows(db_session, 12)
    response = await async_client.put(f"/api/v1/auto-queue/{item_id}", json={"layer_inspect": True})
    assert response.status_code == 200, response.text
    assert (await row(item_id)).layer_inspect is True


async def test_a_batch_edit_with_one_copy_placed_meanwhile_saves_nothing(async_client, db_session, monkeypatch):
    first, second = await router_rows(db_session, 21, 22, batch_id="b-1")
    before_the_apply(monkeypatch, lambda: placed_meanwhile(second))
    response = await async_client.put("/api/v1/auto-queue/batch/b-1", json={"layer_inspect": True})
    assert response.status_code == 409, response.text
    assert ((await row(first)).layer_inspect, (await row(second)).layer_inspect) == (False, False)


async def test_a_reorder_leaves_a_row_placed_meanwhile_where_it_was(async_client, db_session, monkeypatch):
    first, second = await router_rows(db_session, 31, 32)
    before_the_apply(monkeypatch, lambda: placed_meanwhile(first))
    response = await async_client.post(
        "/api/v1/auto-queue/reorder", json={"items": [{"id": second, "position": 1}, {"id": first, "position": 2}]}
    )
    assert response.status_code == 200, response.text
    assert ((await row(first)).position, (await row(second)).position) == (31, 1)


async def test_a_retry_of_a_row_whose_id_was_reused_changes_nothing(async_client, db_session, monkeypatch):
    from backend.app.api.routes import auto_queue as route
    from backend.app.services.filament_requirements import PrintRequirements

    (item_id,) = await router_rows(db_session, 41)
    async with database.async_session() as db:
        await db.execute(update(AutoQueueItem).where(AutoQueueItem.id == item_id).values(status="failed"))
        await db.commit()

    async def revalidated_then_reused(db, item, *args, **kwargs):
        async with database.async_session() as other:
            await other.execute(
                update(AutoQueueItem).where(AutoQueueItem.id == item_id).values(recovery_identity="another-incarnation")
            )
            await other.commit()
        return PrintRequirements(status="ok")

    monkeypatch.setattr(route, "read_item_requirements", revalidated_then_reused)
    response = await async_client.post(f"/api/v1/auto-queue/{item_id}/retry")
    assert response.status_code == 409, response.text
    assert (await row(item_id)).status == "failed"
