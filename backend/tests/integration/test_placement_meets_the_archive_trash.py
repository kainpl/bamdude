"""An AutoQueue placement of an archive-sourced job meets the trash of that archive, both orders (G03 B, A17 / A19 /
A24; acceptance TA32 F2).

The placement takes the router row's archive A ``FOR KEY SHARE`` in its parent's place, before the router row and the
copy (A24); the trash takes A ``FOR UPDATE`` with the router rows of its footprint. Forward: the placement holds A;
the trash waits on A, then finds a copy of the row in a lane it did not plan -- 409 ``queue_selection_changed``, A not
trashed. Reverse: the trash holds A and the router row; the placement waits, then finds its row gone -- nothing
placed. Never a cycle.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services import auto_queue_scheduler as scheduler_module
from backend.tests.integration.lane_pairs import a_waiter_appears, bypass_the_process_lane, held_after, hold_until
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401
from backend.tests.integration.test_auto_queue_scheduler import _make_printer_with_queue
from backend.tests.integration.test_auto_queue_tick_addresses_its_rows import printers_up

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

ANY_WAIT = ("transactionid", "tuple", "advisory")


async def an_archive_sourced_job(db_session, printer_factory, tmp_path) -> tuple[int, int, int]:
    from backend.tests.fixtures.filament_routing_cases import write_routing_3mf

    printer, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
    path = write_routing_3mf(
        tmp_path / "archived.3mf",
        {1: [{"id": 1, "type": "PLA", "color": "#FFFFFF", "used_g": "1"}]},
        model="A1MINI",
    )
    archive = PrintArchive(
        printer_id=printer.id,
        filename=path.name,
        file_path=str(path),
        file_size=path.stat().st_size,
        print_name="Archived",
        status="completed",
    )
    db_session.add(archive)
    await db_session.flush()
    job = AutoQueueItem(archive_id=archive.id, target_model="A1MINI", status="pending", position=1)
    db_session.add(job)
    await db_session.commit()
    return printer.id, archive.id, job.id


def held_on_the_archive(monkeypatch):
    """The placement stopped right after its archive parent's KEY SHARE."""
    reached, release = asyncio.Event(), asyncio.Event()
    real = scheduler_module._parent_held

    async def held(db, model, row_id):
        result = await real(db, model, row_id)
        if model is PrintArchive and row_id is not None and not reached.is_set():
            await hold_until(reached, release)
        return result

    monkeypatch.setattr(scheduler_module, "_parent_held", held)
    return reached, release


@pytest.fixture
def tick(test_engine, monkeypatch):
    monkeypatch.setattr(scheduler_module, "async_session", async_sessionmaker(test_engine, expire_on_commit=False))
    return lambda: scheduler_module.AutoQueueScheduler().tick()


async def copies_of(factory, job_id):
    async with factory() as db:
        return list(await db.scalars(select(PrintQueueItem).where(PrintQueueItem.source_auto_item_id == job_id)))


async def test_the_trash_meeting_a_placement_that_holds_the_archive_waits_then_refuses(
    test_engine, db_session, printer_factory, async_client, tick, tmp_path, monkeypatch
):
    printer_id, archive_id, job_id = await an_archive_sourced_job(db_session, printer_factory, tmp_path)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    bypass_the_process_lane(test_engine, monkeypatch, printer_id)
    reached, release = held_on_the_archive(monkeypatch)
    trash = None
    with printers_up(printer_id):
        placing = asyncio.create_task(tick())
        try:
            await asyncio.wait_for(reached.wait(), 15)
            trash = asyncio.create_task(async_client.delete(f"/api/v1/archives/{archive_id}"))
            await a_waiter_appears(test_engine, factory, trash, on=ANY_WAIT, lane=printer_id, archive_id=archive_id)
            release.set()
            await asyncio.wait_for(placing, 20)
            response = await asyncio.wait_for(trash, 20)
        finally:
            release.set()
            await asyncio.gather(placing, *([trash] if trash else []), return_exceptions=True)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "queue_selection_changed"
    assert [copy.archive_id for copy in await copies_of(factory, job_id)] == [archive_id]
    async with factory() as db:
        assert (await db.get(PrintArchive, archive_id)).deleted_at is None


async def test_a_placement_meeting_the_trash_that_holds_the_archive_waits_then_places_nothing(
    test_engine, db_session, printer_factory, async_client, tick, tmp_path, monkeypatch
):
    from backend.app.services import archive_delete_scope

    printer_id, archive_id, job_id = await an_archive_sourced_job(db_session, printer_factory, tmp_path)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    bypass_the_process_lane(test_engine, monkeypatch, printer_id)
    reached, release = held_after(monkeypatch, archive_delete_scope.ArchiveDeletePlan, "recheck")
    trash = asyncio.create_task(async_client.delete(f"/api/v1/archives/{archive_id}"))
    placing = None
    with printers_up(printer_id):
        try:
            await asyncio.wait_for(reached.wait(), 10)
            placing = asyncio.create_task(tick())
            await a_waiter_appears(test_engine, factory, placing, on=ANY_WAIT, lane=printer_id, archive_id=archive_id)
            release.set()
            response = await asyncio.wait_for(trash, 20)
            await asyncio.wait_for(placing, 20)
        finally:
            release.set()
            await asyncio.gather(trash, *([placing] if placing else []), return_exceptions=True)
    assert response.status_code == 200, response.text
    assert await copies_of(factory, job_id) == [], "the placement found its row gone and placed nothing"
    async with factory() as db:
        job = await db.get(AutoQueueItem, job_id)
    assert job is None or job.status != "assigned"
