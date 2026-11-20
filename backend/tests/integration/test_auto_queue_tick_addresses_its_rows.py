"""The AutoQueue pass writes only the rows it decided about, and holds nothing across a placement (G03 B, A17/A23).

Acceptance TA27 on SQLite: the pass keeps a frozen view of each router row (id, incarnation, revision) taken at the
fetch; its failure, its waiting reasons and its back-fills are written only on a match under the row's lock, and a
placement compares the row under its lock with the view before any write. An edit between the decision and the
placement — a staged start, a plate, a routing policy, a schedule — places nothing; ``assign-now`` answers 409.
The eligibility's back-fills are memory until the row's own placement or the end batch writes them.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.orm.attributes import set_committed_value

from backend.app.core import database
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services import auto_queue_scheduler as scheduler_module
from backend.app.services.auto_queue_eligibility import router_row_view
from backend.app.services.filament_intake import fail_auto_source
from backend.tests.integration.test_auto_queue_scheduler import (  # noqa: F401 -- fixtures
    _make_printer_with_queue,
    _patch_printer_manager,
    routing_item,
    scheduler,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def printers_up(*printer_ids):
    stack = ExitStack()
    for p in _patch_printer_manager(set(printer_ids)):
        stack.enter_context(p)
    stack.enter_context(
        patch("backend.app.services.auto_queue_scheduler.ws_manager.send_queue_changed", new_callable=AsyncMock)
    )
    return stack


async def row(item_id: int) -> AutoQueueItem | None:
    async with database.async_session() as db:
        return await db.get(AutoQueueItem, item_id)


async def printer_rows() -> int:
    async with database.async_session() as db:
        return await db.scalar(select(func.count()).select_from(PrintQueueItem))


def after_the_decision(monkeypatch, change, *, once=True):
    """``change(db, item)`` runs right after the eligibility chose a printer — between the decision and the
    placement's transaction — on the tick's own session, as another writer's commit would land there."""
    real = scheduler_module.find_eligible_printer
    fired = []

    async def decided(db, item, *args, **kwargs):
        result = await real(db, item, *args, **kwargs)
        if result.printer is not None and (not once or not fired):
            fired.append(item.id)
            await change(db, item)
        return result

    monkeypatch.setattr(scheduler_module, "find_eligible_printer", decided)
    return fired


def a_committed_edit(**values):
    async def change(db, item):
        await db.execute(
            update(AutoQueueItem)
            .where(AutoQueueItem.id == item.id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        await db.commit()

    return change


# --- the addressed failure ----------------------------------------------------------------------------------------


async def test_a_failure_writes_only_the_row_it_was_decided_about(db_session, routing_item):
    item = routing_item(target_model="A1MINI", status="pending", position=1)
    db_session.add(item)
    await db_session.commit()
    view = router_row_view(item)
    assert await fail_auto_source(db_session, view, "source_missing") == "written"
    assert (await row(item.id)).status == "failed"


async def test_a_failure_of_a_row_refreshed_meanwhile_writes_nothing(db_session, routing_item):
    item = routing_item(target_model="A1MINI", status="pending", position=1)
    db_session.add(item)
    await db_session.commit()
    view = router_row_view(item)
    async with database.async_session() as other:
        await other.execute(update(AutoQueueItem).where(AutoQueueItem.id == item.id).values(force_color_match=True))
        await other.commit()
    assert await fail_auto_source(db_session, view, "source_missing") == "stale"
    current = await row(item.id)
    assert (current.status, current.force_color_match) == ("pending", True)


async def test_a_failure_of_a_row_whose_id_was_reused_leaves_the_new_row(db_session, routing_item):
    item = routing_item(target_model="A1MINI", status="pending", position=1)
    db_session.add(item)
    await db_session.commit()
    view = router_row_view(item)
    async with database.async_session() as other:
        await other.execute(
            update(AutoQueueItem).where(AutoQueueItem.id == item.id).values(recovery_identity="another-incarnation")
        )
        await other.commit()
    assert await fail_auto_source(db_session, view, "source_missing") == "gone"
    assert (await row(item.id)).status == "pending"


async def test_a_failure_of_a_row_placed_meanwhile_leaves_it_assigned(db_session, routing_item):
    item = routing_item(target_model="A1MINI", status="pending", position=1)
    db_session.add(item)
    await db_session.commit()
    view = router_row_view(item)
    async with database.async_session() as other:
        await other.execute(update(AutoQueueItem).where(AutoQueueItem.id == item.id).values(status="assigned"))
        await other.commit()
    assert await fail_auto_source(db_session, view, "source_missing") == "gone"
    assert (await row(item.id)).status == "assigned"


# --- an edit between the decision and the placement ---------------------------------------------------------------


@pytest.mark.parametrize(
    "edit",
    [
        {"manual_start": True},
        {"plate_id": 7},
        {"force_color_match": True},
        {"feed_policy": "external_only"},
        {"filament_overrides": '[{"slot": 1, "type": "PETG"}]'},
        {"scheduled_time": datetime(2099, 1, 1)},
    ],
    ids=["manual_start", "plate", "force_color", "feed_policy", "override", "scheduled_later"],
)
async def test_an_edit_after_the_decision_places_nothing(
    db_session, scheduler, printer_factory, routing_item, monkeypatch, edit
):
    printer, _pq = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
    item = routing_item(target_model="A1MINI", status="pending", position=1)
    db_session.add(item)
    await db_session.commit()
    item_id = item.id
    after_the_decision(monkeypatch, a_committed_edit(**edit))
    with printers_up(printer.id):
        await scheduler.tick()
    current = await row(item_id)
    assert current.status == "pending"
    for name, value in edit.items():
        assert getattr(current, name) == value
    assert await printer_rows() == 0, "nothing placed"


async def test_an_untouched_row_is_placed_from_the_locked_row_not_the_ticks_copy(
    db_session, scheduler, printer_factory, routing_item, monkeypatch
):
    printer, _pq = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
    item = routing_item(target_model="A1MINI", status="pending", position=1, auto_off_after=False)
    db_session.add(item)
    await db_session.commit()
    item_id = item.id

    async def the_ticks_copy_drifts(db, item):
        # Memory only -- the database row still says False.
        set_committed_value(item, "auto_off_after", True)

    after_the_decision(monkeypatch, the_ticks_copy_drifts)
    with printers_up(printer.id):
        await scheduler.tick()
    assert (await row(item_id)).status == "assigned"
    async with database.async_session() as db:
        copy = (await db.execute(select(PrintQueueItem))).scalar_one()
    assert copy.auto_off_after is False, "the copy is built from the row re-read under its lock"


async def test_the_claim_carries_the_routers_admission_at_its_moment(
    db_session, scheduler, printer_factory, routing_item, monkeypatch
):
    """Defence in depth: with the revision check bypassed, the CAS's router_view_conditions(now) still refuses a
    row scheduled for later."""
    printer, _pq = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
    item = routing_item(target_model="A1MINI", status="pending", position=1)
    db_session.add(item)
    await db_session.commit()
    item_id = item.id
    monkeypatch.setattr(scheduler_module, "router_row_outcome", lambda row, view: "written")
    after_the_decision(monkeypatch, a_committed_edit(scheduled_time=datetime(2099, 1, 1)))
    with printers_up(printer.id):
        await scheduler.tick()
    assert (await row(item_id)).status == "pending"
    assert await printer_rows() == 0


async def test_assign_now_of_a_row_edited_before_its_placement_is_refused(
    async_client, db_session, printer_factory, routing_item, monkeypatch
):
    from backend.app.api.routes import auto_queue as route

    printer, _pq = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
    item = routing_item(target_model="A1MINI", status="pending", position=1, manual_start=True)
    db_session.add(item)
    await db_session.commit()
    item_id = item.id
    real = route.find_eligible_printer

    async def decided(db, item, *args, **kwargs):
        result = await real(db, item, *args, **kwargs)
        async with database.async_session() as other:
            await other.execute(update(AutoQueueItem).where(AutoQueueItem.id == item_id).values(plate_id=9))
            await other.commit()
        return result

    monkeypatch.setattr(route, "find_eligible_printer", decided)
    with printers_up(printer.id):
        response = await async_client.post(f"/api/v1/auto-queue/{item_id}/assign-now")
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"] == "queue_item_changed"
    assert (await row(item_id)).status == "pending"
    assert await printer_rows() == 0


# --- the waiting batch and the back-fills -------------------------------------------------------------------------


async def test_the_waiting_batch_writes_nothing_on_a_row_edited_meanwhile(
    db_session, scheduler, routing_item, monkeypatch
):
    item = routing_item(target_model="A1MINI", status="pending", position=1)
    db_session.add(item)
    await db_session.commit()
    item_id = item.id
    real = scheduler_module.find_eligible_printer

    async def waited(db, item, *args, **kwargs):
        result = await real(db, item, *args, **kwargs)
        await a_committed_edit(force_color_match=True)(db, item)
        return result

    monkeypatch.setattr(scheduler_module, "find_eligible_printer", waited)
    with printers_up():
        await scheduler.tick()
    current = await row(item_id)
    assert (current.waiting_reason, current.force_color_match) == (None, True)
    assert scheduler._active_waiting_facts == set(), "an incomplete pass resolves and replaces no keys"


async def test_the_back_fills_of_an_unplaced_row_are_written_by_the_end_batch(db_session, scheduler, routing_item):
    item = routing_item(status="pending", position=1)  # no target model: the eligibility back-fills it
    db_session.add(item)
    await db_session.commit()
    item_id = item.id
    with printers_up():
        await scheduler.tick()
    current = await row(item_id)
    assert current.target_model and current.plate_id == 1
    assert current.waiting_reason


async def test_the_back_fills_of_a_placed_row_are_written_by_its_own_claim(
    db_session, scheduler, printer_factory, routing_item
):
    printer, _pq = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
    item = routing_item(status="pending", position=1)
    db_session.add(item)
    await db_session.commit()
    item_id = item.id
    with printers_up(printer.id):
        await scheduler.tick()
    current = await row(item_id)
    assert current.status == "assigned"
    assert current.target_model and current.plate_id == 1


async def test_no_back_fill_of_a_waiting_row_lands_in_another_rows_placement(
    db_session, scheduler, printer_factory, routing_item, test_engine, tmp_path
):
    from backend.app.models.library import LibraryFile
    from backend.tests.fixtures.filament_routing_cases import write_routing_3mf

    printer, _pq = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
    # No model named: the eligibility back-fills it and the plate, then finds no printer holding PETG -- it waits,
    # while the next row (PLA) is placed.
    path = write_routing_3mf(
        tmp_path / "petg.3mf", {1: [{"id": 1, "type": "PETG", "color": "#FFFFFF", "used_g": "1"}]}, model="A1MINI"
    )
    waiting = AutoQueueItem(
        library_file=LibraryFile(
            filename=path.name, file_path=str(path), file_type="gcode", file_size=path.stat().st_size
        ),
        status="pending",
        position=1,
    )
    placed = routing_item(target_model="A1MINI", status="pending", position=2)
    db_session.add_all([waiting, placed])
    await db_session.commit()
    waiting_id, placed_id = waiting.id, placed.id
    statements: list[tuple[str, object]] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append((statement, parameters))

    event.listen(test_engine.sync_engine, "before_cursor_execute", record)
    try:
        with printers_up(printer.id):
            await scheduler.tick()
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", record)
    insert_at = next(
        i for i, (s, _p) in enumerate(statements) if s.lstrip().upper().startswith("INSERT INTO PRINT_QUEUE")
    )
    early = [
        s
        for s, params in statements[:insert_at]
        if s.lstrip().upper().startswith("UPDATE AUTO_QUEUE_ITEMS")
        and "plate_id" in s
        and _updated_id(s, params) == waiting_id
    ]
    assert early == [], "the waiting row's back-fill was flushed inside another row's placement"
    assert (await row(placed_id)).status == "assigned"
    current = await row(waiting_id)
    assert current.status == "pending"
    assert current.target_model and current.plate_id == 1, "the end batch wrote the waiting row's back-fills"


def _updated_id(statement: str, params) -> int | None:
    """The row an ``UPDATE auto_queue_items SET ... WHERE auto_queue_items.id = ?`` statement addresses."""
    head, _, where = statement.partition(" WHERE ")
    if not where.lstrip().startswith("auto_queue_items.id = ?"):
        return None
    return tuple(params or ())[head.count("?")]


async def test_after_a_refused_placement_the_next_row_is_read_fresh_and_placed(
    db_session, scheduler, printer_factory, routing_item, monkeypatch
):
    printer, _pq = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
    first = routing_item(target_model="A1MINI", status="pending", position=1)
    second = routing_item(target_model="A1MINI", status="pending", position=2)
    db_session.add_all([first, second])
    await db_session.commit()
    first_id, second_id = first.id, second.id
    real = scheduler_module.swap_basis_holds
    refused = []

    async def holds(db, item, basis):
        if item.id == first_id:
            refused.append(item.id)
            return False  # inside the lane, after the claim: the placement rolls back
        return await real(db, item, basis)

    monkeypatch.setattr(scheduler_module, "swap_basis_holds", holds)
    with printers_up(printer.id):
        await scheduler.tick()
    assert refused == [first_id]
    assert (await row(first_id)).status == "pending", "the refused placement wrote nothing"
    assert (await row(second_id)).status == "assigned"


async def test_a_printer_lost_under_its_lane_passes_the_job_to_the_next_candidate(
    db_session, scheduler, printer_factory, routing_item, monkeypatch
):
    """A refusal read under the placement's lane (a job committed into the chosen printer's queue after the
    decision: ``pending_backlog``) rolls the placement back -- which expires the pass's Printer rows. The pass keeps
    the candidate's id as a value, excludes it and places on the next printer; it does not die reading the expired
    row (found by TA21 on PostgreSQL)."""
    first, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
    second, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
    job = routing_item(target_model="A1MINI", status="pending", position=1)
    db_session.add(job)
    await db_session.commit()
    job_id, printer_ids = job.id, {first.id, second.id}
    lost: list[int] = []
    real = scheduler_module.find_eligible_printer

    async def decided(db, item, *args, **kwargs):
        result = await real(db, item, *args, **kwargs)
        if result.printer is not None and not lost:
            lost.append(result.printer.id)
            db.add(PrintQueueItem(queue_id=result.printer.id, position=1, status="pending"))
            await db.commit()
        return result

    monkeypatch.setattr(scheduler_module, "find_eligible_printer", decided)
    with printers_up(*printer_ids):
        await scheduler.tick()
    placed = await row(job_id)
    assert placed.status == "assigned"
    async with database.async_session() as db:
        copy = await db.scalar(select(PrintQueueItem).where(PrintQueueItem.source_auto_item_id == job_id))
    assert copy.queue_id == (printer_ids - set(lost)).pop()


async def test_the_runners_requirements_read_leaves_no_pending_plate_write(db_session, printer_factory):
    from types import SimpleNamespace

    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.services.print_scheduler import PrintScheduler

    printer = await printer_factory()
    db_session.add(PrinterQueue(id=printer.id, printer_id=printer.id))
    item = PrintQueueItem(queue_id=printer.id, status="pending", position=0)
    db_session.add(item)
    await db_session.commit()
    requirements = SimpleNamespace(status="ok", resolved_plate_id=3, used_filaments=())
    with patch("backend.app.services.filament_intake.read_item_requirements", new=AsyncMock(return_value=requirements)):
        await PrintScheduler()._get_filament_requirements(db_session, item)
    assert item.plate_id == 3
    assert not db_session.is_modified(item), "a memory back-fill, never a pending write"
