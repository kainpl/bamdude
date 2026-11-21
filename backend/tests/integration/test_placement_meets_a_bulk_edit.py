"""An AutoQueue placement meets a bulk edit holding the target's lane (G03 B, A17; acceptance TA21, single lane).

The real tick decides P1 (P2 is busy with a waiting job X); right then the real bulk PATCH moves X to P1 and is held
inside its lanes. PostgreSQL: the placement waits on P1's lane guard -- never a cycle, never a hang -- and, once the
edit committed, places or refuses on what it then reads. SQLite: a lane held in process is the printer lost for the
pass (``lane_busy``), never a wait behind it. Either way the AutoQueue row ends pending or assigned to P1, and the edit
commits.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services import auto_queue_scheduler as scheduler_module, queue_ops
from backend.tests.integration.lane_pairs import a_waiter_appears, bypass_the_process_lane, held_inside
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401
from backend.tests.integration.test_auto_queue_scheduler import (  # noqa: F401 -- fixtures
    _make_printer_with_queue,
    routing_item,
)
from backend.tests.integration.test_auto_queue_tick_addresses_its_rows import printers_up

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_a_placement_meeting_a_bulk_edit_on_its_lane_waits_or_gives_the_printer_up(
    test_engine, db_session, printer_factory, routing_item, async_client, monkeypatch, tmp_path
):
    from backend.app.schemas.print_queue import PrintQueueItemCreate
    from backend.app.services.printer_manager import printer_manager
    from backend.app.services.queue_add import add_items_to_printer_queue
    from backend.tests.integration.test_filament_routing_dispatch import setup_source

    # P2 holds a captured job X (a real add, so the move to P1 can re-validate its bytes); P1 is the same model, free.
    source, p2, _queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    p1, _ = await _make_printer_with_queue(db_session, printer_factory, model="P1P")
    monkeypatch.setitem(printer_manager._models, p1.id, "P1P")
    added, _header = await add_items_to_printer_queue(
        db_session, PrintQueueItemCreate(queue_id=p2.id, library_file_id=source.id, plate_id=15), None
    )
    moved_id = added[0].id
    job = routing_item(target_model="P1P", status="pending", position=1)
    db_session.add(job)
    await db_session.commit()
    job_id = job.id
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr(scheduler_module, "async_session", factory)
    for printer_id in (p1.id, p2.id):
        bypass_the_process_lane(test_engine, monkeypatch, printer_id)

    real = scheduler_module.find_eligible_printer
    edit: list[asyncio.Task] = []
    held: list[tuple[asyncio.Event, asyncio.Event]] = []

    calls: list = []

    async def decided(db, item, *args, **kwargs):
        result = await real(db, item, *args, **kwargs)
        calls.append((item.id, getattr(result.printer, "id", None), getattr(result, "reason", None)))
        if result.printer is not None and not edit:
            assert result.printer.id == p1.id, "P2 is busy with X"
            reached, release = held_inside(monkeypatch, queue_ops, "claim_lanes")
            held.append((reached, release))
            edit.append(
                asyncio.create_task(
                    async_client.patch("/api/v1/queue/bulk", json={"item_ids": [moved_id], "queue_id": p1.id})
                )
            )
            await asyncio.wait_for(reached.wait(), 10)
        return result

    monkeypatch.setattr(scheduler_module, "find_eligible_printer", decided)
    real_held = scheduler_module.lane_held_in_process
    given_up = asyncio.Event()

    def held_in_process(db, queue_id):
        answer = real_held(db, queue_id)
        if answer:
            given_up.set()
        return answer

    monkeypatch.setattr(scheduler_module, "lane_held_in_process", held_in_process)
    tick = None
    with printers_up(p1.id, p2.id):
        try:
            tick = asyncio.create_task(scheduler_module.AutoQueueScheduler().tick())
            async with asyncio.timeout(20):
                while not held:
                    if tick.done():
                        pytest.fail(f"the pass ended before its decision: {tick.exception()!r} {calls}")
                    await asyncio.sleep(0.02)
            await asyncio.wait_for(held[0][0].wait(), 10)
            if test_engine.dialect.name == "postgresql":
                await a_waiter_appears(test_engine, factory, tick, p1.id)
            else:
                # SQLite: the placement gives the held lane up for the pass instead of waiting behind it. (The pass's
                # end batch then waits for SQLite's writer, as any short write does -- so the edit is let go now.)
                await asyncio.wait_for(given_up.wait(), 10)
            held[0][1].set()
            response = await asyncio.wait_for(edit[0], 20)
            await asyncio.wait_for(tick, 20)
        finally:
            for _reached, release in held:
                release.set()
            await asyncio.gather(*edit, *([tick] if tick else []), return_exceptions=True)
    assert response.status_code == 200, response.text
    async with factory() as db:
        assert (await db.get(PrintQueueItem, moved_id)).queue_id == p1.id
        router = await db.get(AutoQueueItem, job_id)
        assert router.status in {"pending", "assigned"}, router.status
        copies = list(
            await db.scalars(select(PrintQueueItem.queue_id).where(PrintQueueItem.source_auto_item_id == job_id))
        )
        assert copies in ([], [p1.id]), copies
        assert (router.status == "assigned") == bool(copies)
