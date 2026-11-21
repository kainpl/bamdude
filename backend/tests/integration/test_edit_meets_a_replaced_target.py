"""A move lands only in the header its checks judged, and Manual start writes only the group it read (G03 B, A6,
A7, A25; review B1-01, B1-04).

The edit's checks read the target queue and its printer once (``checked_facts``); the printer is deleted through the
real DELETE -- right after that snapshot, before the first check that depends on it, or after all the checks -- and,
in the second shape, a new printer takes its id with a new queue header. The single and batch edits answer 409
``queue_item_changed`` and the job stays where it was, the bulk edit skips it; an unchanged target is the control. Manual start whose clicked row left its group between the read and the lane writes nothing.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from sqlalchemy import update

from backend.app.core import database
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import queue_ops
from backend.tests.integration.test_edit_applies_under_the_lane import pending_rows, row
from backend.tests.integration.test_queue_reuse_keeps_the_bytes import (  # noqa: F401 - fixtures
    a_captured_item,
    clean_spool_state,
    sessions,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

CASES = [
    (None, "after_checks"),
    ("deleted", "after_snapshot"),
    ("replaced", "after_snapshot"),
    ("deleted", "after_checks"),
    ("replaced", "after_checks"),
]


async def a_target(db_session, printer_factory):
    target = await printer_factory(model="P1P")
    header = PrinterQueue(id=target.id, printer_id=target.id)
    db_session.add(header)
    await db_session.commit()
    return target.id, header.recovery_identity


def the_target_changes(monkeypatch, async_client, printer_factory, target_id, old_identity, change, when):
    """The target is deleted (and replaced) right after the snapshot the checks are judged by (``checked_facts``),
    or after the checks (``routing_update`` is the last of them)."""
    from backend.app.api.routes import print_queue

    name = "checked_facts" if when == "after_snapshot" else "routing_update"
    real = getattr(print_queue, name)
    fired = []

    async def validation(*args, **kwargs):
        values = await real(*args, **kwargs)
        if change is not None and not fired:
            fired.append(True)
            deleted = await async_client.delete(f"/api/v1/printers/{target_id}?delete_archives=false")
            assert deleted.status_code == 200, deleted.text
            if change == "replaced":
                # SQLite would hand the next printer this id by itself; PostgreSQL's sequence never does.
                replacement = await printer_factory(model="P1P", id=target_id)
                assert replacement.id == target_id
                async with database.async_session() as db:
                    header = PrinterQueue(id=target_id, printer_id=target_id)
                    db.add(header)
                    await db.commit()
                    assert header.recovery_identity != old_identity
        return values

    monkeypatch.setattr(print_queue, name, validation)
    return fired


async def a_second_member(item, batch_id: str) -> int:
    """A copy of the captured job beside it in the same queue, both in one batch."""
    async with database.async_session() as db:
        sibling = PrintQueueItem(
            queue_id=item.queue_id,
            status="pending",
            position=1,
            queue_source_id=item.queue_source_id,
            source_snapshot=item.source_snapshot,
            library_file_id=item.library_file_id,
            plate_id=item.plate_id,
            filament_routing=item.filament_routing,
            batch_id=batch_id,
        )
        db.add(sibling)
        await db.execute(update(PrintQueueItem).where(PrintQueueItem.id == item.id).values(batch_id=batch_id))
        await db.commit()
        return sibling.id


@pytest.mark.parametrize(("change", "when"), CASES)
async def test_a_single_move_lands_only_in_the_header_it_checked(
    async_client, db_session, tmp_path, printer_factory, monkeypatch, sessions, change, when
):
    item, _printer, source, _file, _original = await a_captured_item(db_session, tmp_path, printer_factory, monkeypatch)
    source_id, item_id = source.id, item.id
    target_id, identity = await a_target(db_session, printer_factory)
    fired = the_target_changes(monkeypatch, async_client, printer_factory, target_id, identity, change, when)
    response = await async_client.patch(f"/api/v1/queue/{item_id}", json={"queue_id": target_id})
    assert bool(fired) == (change is not None)
    if change is None:
        assert response.status_code == 200, response.text
        assert (await row(item_id)).queue_id == target_id
    else:
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["error"] == "queue_item_changed"
        assert (await row(item_id)).queue_id == source_id


@pytest.mark.parametrize(("change", "when"), CASES)
async def test_a_bulk_move_skips_a_job_whose_target_changed_after_its_checks(
    async_client, db_session, tmp_path, printer_factory, monkeypatch, sessions, change, when
):
    item, _printer, source, _file, _original = await a_captured_item(db_session, tmp_path, printer_factory, monkeypatch)
    source_id, item_id = source.id, item.id
    target_id, identity = await a_target(db_session, printer_factory)
    the_target_changes(monkeypatch, async_client, printer_factory, target_id, identity, change, when)
    response = await async_client.patch("/api/v1/queue/bulk", json={"item_ids": [item_id], "queue_id": target_id})
    assert response.status_code == 200, response.text
    body = response.json()
    if change is None:
        assert (body["updated_count"], body["skipped_count"]) == (1, 0)
        assert (await row(item_id)).queue_id == target_id
    else:
        assert (body["updated_count"], body["skipped_count"]) == (0, 1)
        assert (await row(item_id)).queue_id == source_id


@pytest.mark.parametrize(("change", "when"), CASES)
async def test_a_batch_move_lands_only_in_the_header_it_checked(
    async_client, db_session, tmp_path, printer_factory, monkeypatch, sessions, change, when
):
    item, _printer, source, _file, _original = await a_captured_item(db_session, tmp_path, printer_factory, monkeypatch)
    source_id, item_id = source.id, item.id
    sibling_id = await a_second_member(item, "moving-batch")
    target_id, identity = await a_target(db_session, printer_factory)
    the_target_changes(monkeypatch, async_client, printer_factory, target_id, identity, change, when)
    response = await async_client.patch(
        f"/api/v1/queue/batch/moving-batch?queue_id={source_id}", json={"queue_id": target_id}
    )
    queues = ((await row(item_id)).queue_id, (await row(sibling_id)).queue_id)
    if change is None:
        assert response.status_code == 200, response.text
        assert queues == (target_id, target_id)
    else:
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["error"] == "queue_item_changed"
        assert queues == (source_id, source_id)


@pytest.mark.parametrize("regroup", ["ungroup", "join"])
async def test_manual_start_writes_nothing_when_the_group_changed_before_its_lane(
    async_client, db_session, printer_factory, monkeypatch, regroup
):
    """B1-04: the clicked row's group as read is the one the toggle writes, or nothing is written — former members
    keep their flag and the answer never reports a value the clicked row did not get."""
    if regroup == "ungroup":
        _queue, (clicked, sibling) = await pending_rows(db_session, printer_factory, count=2, batch_id="old")
    else:
        _queue, (clicked, sibling) = await pending_rows(db_session, printer_factory, count=2)
    queue_id = (await row(clicked)).queue_id
    real_lane_session = queue_ops.lane_session
    armed = True

    @asynccontextmanager
    async def regroup_before_the_lane(queue_ids):
        nonlocal armed
        if armed:
            armed = False
            if regroup == "ungroup":
                response = await async_client.post(f"/api/v1/queue/batch/old/ungroup?queue_id={queue_id}")
            else:
                response = await async_client.post("/api/v1/queue/batch", json={"item_ids": [clicked, sibling]})
            assert response.status_code == 200, response.text
        async with real_lane_session(queue_ids) as db:
            yield db

    monkeypatch.setattr(queue_ops, "lane_session", regroup_before_the_lane)
    response = await async_client.patch(f"/api/v1/queue/{clicked}/manual-start")
    assert not armed
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"] == "queue_item_changed"
    assert ((await row(clicked)).manual_start, (await row(sibling)).manual_start) == (False, False)


async def test_manual_start_toggles_the_group_it_read(async_client, db_session, printer_factory):
    """The control: an unchanged group follows the clicked row, and the answer is what the row got."""
    _queue, (clicked, sibling) = await pending_rows(db_session, printer_factory, count=2, batch_id="kept")
    response = await async_client.patch(f"/api/v1/queue/{clicked}/manual-start")
    assert response.status_code == 200, response.text
    assert response.json() == {"manual_start": True, "item_id": clicked}
    assert ((await row(clicked)).manual_start, (await row(sibling)).manual_start) == (True, True)
