"""G03 B3: routing and claiming on the same calendar, on SQLite and PostgreSQL."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.project import Project
from backend.app.services import (
    auto_queue_scheduler as router,
    order_resources,
    print_scheduler as print_pass,
    printer_occupancy,
    queue_rebalance,
)
from backend.app.services.auto_queue_eligibility import busy_printer_ids
from backend.app.services.queue_counters import set_queue_idle
from backend.app.services.queue_row_basis import row_basis
from backend.tests.integration.lane_pairs import ROW_WAIT, postgres_edge
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401
from backend.tests.integration.test_auto_queue_scheduler import (
    _make_printer_with_queue,
    _patch_printer_manager,
    routing_item,  # noqa: F401
    scheduler,  # noqa: F401
)

pytestmark = pytest.mark.integration
T = datetime(2030, 5, 1, 8, tzinfo=timezone.utc)
LATER = T + timedelta(minutes=5)
BEFORE = T - timedelta(seconds=1)


@pytest.fixture
def clock(monkeypatch):
    class Clock(datetime):
        time = BEFORE
        reads = 0

        @classmethod
        def now(cls, tz=None):
            cls.reads += 1
            return cls.time.astimezone(tz) if tz else cls.time.replace(tzinfo=None)

    for module in (print_pass, router, printer_occupancy):
        monkeypatch.setattr(module, "datetime", Clock)
    return Clock


@pytest.fixture
def dispatcher(monkeypatch, test_engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr(print_pass, "async_session", factory)
    return print_pass.PrintScheduler()


async def claim(dispatcher, job):
    queue_id = job.queue_id
    return await dispatcher._claim_for_dispatch(
        item_id=job.id, queue_id=queue_id, printer_id=queue_id, plan_fields={}, basis=row_basis(job)
    )


async def complete(db, job):
    # Synthetic device completion. Release only the claim already proved by the scheduler.
    job.status = "completed"
    await set_queue_idle(db, job.queue_id, expected_item_id=job.id)
    await db.commit()


async def printer_rows(db, printer_id):
    query = select(PrintQueueItem).where(PrintQueueItem.queue_id == printer_id).order_by(PrintQueueItem.position)
    return list(await db.scalars(query.execution_options(populate_existing=True)))


@pytest.mark.parametrize("claim_before", [True, False])
async def test_scheduled_printer_handoff(
    db_session, printer_factory, routing_item, scheduler, dispatcher, clock, claim_before
):
    printer, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
    scheduled = PrintQueueItem(queue_id=printer.id, position=1, scheduled_time=T)
    first, second = [routing_item(target_model="A1MINI", status="pending", position=position) for position in (1, 2)]
    db_session.add_all([scheduled, first, second])
    await db_session.commit()
    elig, sched = _patch_printer_manager({printer.id})
    with elig, sched:
        await scheduler.tick()
        await scheduler.tick()
        assert first.status == "assigned" and second.status == "pending"
        pinned, placed = await printer_rows(db_session, printer.id)
        assert placed.source_auto_item_id == first.id and pinned.id == scheduled.id
        early, late = (placed, pinned) if claim_before else (pinned, placed)
        clock.time = BEFORE if claim_before else T
        assert (await claim(dispatcher, late)).reason == "not_first_runnable"
        assert (await claim(dispatcher, early)).claimed
        await db_session.refresh(early)
        assert early.started_at == clock.time.replace(tzinfo=None)
        clock.time = T
        assert (await claim(dispatcher, late)).reason == "active_claim"
        await scheduler.tick()
        assert second.status == "pending"
        await db_session.refresh(early)
        await complete(db_session, early)
        assert (await claim(dispatcher, late)).claimed


async def test_a_row_due_during_eligibility_refuses_placement(
    db_session, printer_factory, routing_item, scheduler, clock, monkeypatch
):
    printer, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
    row = PrintQueueItem(queue_id=printer.id, position=1, scheduled_time=T)
    item = routing_item(target_model="A1MINI", status="pending", position=1)
    db_session.add_all([row, item])
    await db_session.commit()
    printer_id, item_id = printer.id, item.id
    eligible = router.find_eligible_printer
    require = router.require_auto_placement
    refusals = []

    def require_under_lane(occupancy):
        try:
            require(occupancy)
        except printer_occupancy.PrinterOccupancyConflict as exc:
            refusals.append(exc.code)
            raise

    monkeypatch.setattr(router, "require_auto_placement", require_under_lane)

    async def due_after_eligibility(*args, **kwargs):
        result = await eligible(*args, **kwargs)
        if result.printer is not None:
            clock.time = T
        return result

    monkeypatch.setattr(router, "find_eligible_printer", due_after_eligibility)
    elig, sched = _patch_printer_manager({printer_id})
    with elig, sched:
        await scheduler.tick()
    final = await db_session.get(AutoQueueItem, item_id, populate_existing=True)
    assert final.status == "pending" and final.assigned_to_item_id is None
    assert refusals == ["pending_backlog"]
    assert len(await printer_rows(db_session, printer_id)) == 1


@pytest.mark.parametrize("staged", [False, True])
async def test_deferred_queue_is_a_rank_penalty_and_can_take_work(
    db_session, printer_factory, routing_item, scheduler, clock, staged
):
    reserved, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
    free, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
    db_session.add(
        PrintQueueItem(queue_id=reserved.id, position=1, manual_start=staged, scheduled_time=None if staged else T)
    )
    first, second = [routing_item(target_model="A1MINI", status="pending", position=position) for position in (1, 2)]
    db_session.add_all([first, second])
    await db_session.commit()
    assert reserved.id < free.id
    elig, sched = _patch_printer_manager({reserved.id, free.id})
    with elig, sched:
        now = BEFORE.replace(tzinfo=None)
        ctx = await queue_rebalance._pass_context(
            db_session, now, resources={}, busy=None, interlocked=set(), cache=None
        )
        forecast = await queue_rebalance._load_forecast(db_session, ctx, [], now)
        deferred = forecast.farm.machines[reserved.id]
        assert not deferred.hold_unknown
        assert deferred.has_staged_rows is staged
        if not staged:
            (held,) = deferred.held
            assert held.not_before == 1
        projection = queue_rebalance.start_projection([], forecast.farm)
        for job, target in ((first, free), (second, reserved)):
            result = projection.result_of(job.id)
            assert (result.routed_at[job.id], result.lane_of[job.id]) == (0.0, target.id)
        await scheduler.tick()
    await db_session.refresh(first)
    await db_session.refresh(second)
    assigned = dict(
        (
            await db_session.execute(
                select(PrintQueueItem.source_auto_item_id, PrintQueueItem.queue_id).where(
                    PrintQueueItem.source_auto_item_id.in_((first.id, second.id))
                )
            )
        ).all()
    )
    assert assigned == {first.id: free.id, second.id: reserved.id}


@pytest.mark.parametrize("due", [False, True])
async def test_assign_now_keeps_the_time_and_the_copy_holds_only_when_due(
    db_session, async_client, printer_factory, routing_item, scheduler, dispatcher, clock, due
):
    printer, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
    forced = routing_item(target_model="A1MINI", status="pending", position=1, scheduled_time=T, manual_start=True)
    auto = routing_item(target_model="A1MINI", status="pending", position=2)
    db_session.add_all([forced, auto])
    await db_session.commit()
    clock.time = T if due else BEFORE
    elig, sched = _patch_printer_manager({printer.id})
    with elig, sched:
        response = await async_client.post(f"/api/v1/auto-queue/{forced.id}/assign-now")
        assert response.status_code == 200, response.text
        (copied,) = await printer_rows(db_session, printer.id)
        assert copied.scheduled_time == T.replace(tzinfo=None)
        assert not copied.manual_start
        assert (printer.id in await busy_printer_ids(db_session, clock.time)) is due
        await scheduler.tick()
        await db_session.refresh(auto)
        if due:
            assert auto.status == "pending"
            assert len(await printer_rows(db_session, printer.id)) == 1
            return
        assert auto.status == "assigned"
        copied, placed = await printer_rows(db_session, printer.id)
        assert placed.position > copied.position
        assert (await claim(dispatcher, placed)).claimed
        await db_session.refresh(placed)
        await complete(db_session, placed)
        clock.time = T
        assert (await claim(dispatcher, copied)).claimed


@pytest.mark.parametrize("staged", [False, True])
async def test_wake_candidates_follow_the_same_busy_set(
    db_session, printer_factory, smart_plug_factory, scheduler, routing_item, clock, staged, monkeypatch
):
    printer, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
    job = PrintQueueItem(queue_id=printer.id, position=1, scheduled_time=None if staged else T, manual_start=staged)
    auto = routing_item(target_model="A1MINI", status="pending", position=1)
    db_session.add_all([job, auto])
    plug = await smart_plug_factory(printer_id=printer.id, auto_on=True, enabled=True)
    await db_session.commit()
    service = SimpleNamespace(turn_on=AsyncMock(return_value=True))
    monkeypatch.setattr(
        "backend.app.services.smart_plug_manager.smart_plug_manager.get_service_for_plug",
        AsyncMock(return_value=service),
    )
    monkeypatch.setattr("backend.app.services.smart_plug_manager.plug_switched", AsyncMock())
    elig, sched = _patch_printer_manager(set())
    with elig, sched:
        job.manual_start, job.scheduled_time = False, T
        clock.time = T
        await db_session.commit()
        await scheduler.tick()
        service.turn_on.assert_not_awaited()
        job.manual_start, job.scheduled_time = staged, None if staged else T
        clock.time = BEFORE
        await db_session.commit()
        await scheduler.tick()
        assert service.turn_on.await_args.args[0].id == plug.id
        service.turn_on.assert_awaited_once()


async def test_staging_a_placement_allows_one_more_then_both_claim_in_position_order(
    db_session, async_client, printer_factory, routing_item, scheduler, dispatcher, clock
):
    printer, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
    db_session.add_all(routing_item(target_model="A1MINI", status="pending", position=i) for i in (1, 2, 3))
    await db_session.commit()
    elig, sched = _patch_printer_manager({printer.id})
    with elig, sched:
        await scheduler.tick()
        (first,) = await printer_rows(db_session, printer.id)
        response = await async_client.patch(f"/api/v1/queue/{first.id}", json={"manual_start": True})
        assert response.status_code == 200, response.text
        await scheduler.tick()
        await scheduler.tick()
        first, second = await printer_rows(db_session, printer.id)
        assert first.manual_start
        response = await async_client.post(f"/api/v1/queue/{first.id}/start")
        assert response.status_code == 200, response.text
        first, second = await printer_rows(db_session, printer.id)
        assert not first.manual_start
        assert (await claim(dispatcher, second)).reason == "not_first_runnable"
        for job in (first, second):
            assert (await claim(dispatcher, job)).claimed
            await db_session.refresh(job)
            assert job.dispatch_attempts == 1
            await complete(db_session, job)


async def test_claim_keeps_its_head_query_instant_across_order_wait(
    db_session, test_engine, printer_factory, dispatcher, clock, monkeypatch
):
    printer, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
    order = Project(name="Calendar barrier")
    db_session.add(order)
    await db_session.flush()
    scheduled = PrintQueueItem(queue_id=printer.id, position=1, scheduled_time=T)
    placed = PrintQueueItem(queue_id=printer.id, position=2, project_id=order.id)
    db_session.add_all([scheduled, placed])
    await db_session.commit()
    allowed = order_resources.allowed
    reached = asyncio.Event()
    release = asyncio.Event()

    async def order_guard(*args, **kwargs):
        reached.set()
        if test_engine.dialect.name == "sqlite":
            await release.wait()
        return await allowed(*args, **kwargs)

    monkeypatch.setattr(order_resources, "allowed", order_guard)
    if test_engine.dialect.name == "postgresql":
        await db_session.scalar(select(Project.id).where(Project.id == order.id).with_for_update())
    clock.reads = 0
    task = asyncio.create_task(claim(dispatcher, placed))
    try:
        await asyncio.wait_for(reached.wait(), timeout=5)
        if test_engine.dialect.name == "postgresql":
            from sqlalchemy.ext.asyncio import async_sessionmaker

            await postgres_edge(async_sessionmaker(test_engine), task, on=ROW_WAIT, behind=asyncio.current_task())
        # The head was selected under the lane at BEFORE, but the attempt cannot
        # commit while this order's guard is held.
        assert not task.done()
        clock.time = T + timedelta(seconds=1)
    finally:
        release.set()
        await db_session.commit()
    assert (await asyncio.wait_for(task, timeout=5)).claimed
    # The claim takes one clock reading across both the scheduler and occupancy.
    assert clock.reads == 1
    await db_session.refresh(placed)
    assert placed.started_at == BEFORE.replace(tzinfo=None)
    await complete(db_session, placed)
    assert (await claim(dispatcher, scheduled)).claimed


@pytest.mark.parametrize("running", [False, True])
async def test_due_row_loses_scheduled_reason_behind_a_head_or_running_print(
    db_session, printer_factory, dispatcher, clock, monkeypatch, running
):
    printer, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
    head = PrintQueueItem(queue_id=printer.id, position=1, status="printing" if running else "pending")
    later = PrintQueueItem(
        queue_id=printer.id,
        position=2,
        scheduled_time=T,
        waiting_reason="Scheduled start",
        waiting_reason_code="scheduled",
        waiting_reason_checked_at=BEFORE,
    )
    db_session.add_all([head, later])
    await db_session.commit()
    clock.time = T
    start = AsyncMock(return_value=True)
    monkeypatch.setattr(dispatcher, "_start_print", start)
    monkeypatch.setattr(dispatcher, "_check_auto_drying", AsyncMock())
    monkeypatch.setattr(print_pass, "preflight_item", AsyncMock())
    elig, sched = _patch_printer_manager({printer.id})
    with elig, sched:
        assert await dispatcher.check_queue() is not running
    assert start.await_count == (0 if running else 1)
    await db_session.refresh(later)
    assert (later.waiting_reason, later.waiting_reason_code, later.waiting_reason_checked_at) == (None, None, None)


@pytest.mark.parametrize(
    "first_kind,from_auto_queue",
    [
        ("scheduled", False),
        ("scheduled", True),
        ("staged", False),
        ("staged", True),
        ("due", False),
        ("later", False),
        ("due_running", False),
    ],
)
async def test_only_runnable_head_can_pause_for_full_timelapse_storage(
    db_session,
    printer_factory,
    dispatcher,
    clock,
    monkeypatch,
    first_kind,
    from_auto_queue,
    scheduler,
    routing_item,
):
    printer, queue = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
    first = PrintQueueItem(
        queue_id=printer.id,
        position=1,
        timelapse=first_kind != "later",
        manual_start=first_kind == "staged",
        scheduled_time=T if first_kind == "scheduled" else None,
    )
    second = PrintQueueItem(queue_id=printer.id, position=2, timelapse=first_kind == "later")
    db_session.add(first)
    if from_auto_queue:
        db_session.add(routing_item(target_model="A1MINI", status="pending", position=1, timelapse=False))
    else:
        db_session.add(second)
    if first_kind == "due_running":
        db_session.add(PrintQueueItem(queue_id=printer.id, position=0, status="printing"))
    await db_session.commit()
    elig, sched = _patch_printer_manager({printer.id})
    if from_auto_queue:
        with elig, sched:
            await scheduler.tick()
        first, second = await printer_rows(db_session, printer.id)
        assert second.source_auto_item_id is not None and not second.timelapse
    storage = Mock(return_value=True)
    start = AsyncMock(return_value=True)
    monkeypatch.setattr(print_pass, "_timelapse_storage_full", storage)
    monkeypatch.setattr(print_pass, "preflight_item", AsyncMock())
    monkeypatch.setattr(dispatcher, "_start_print", start)
    monkeypatch.setattr(dispatcher, "_check_auto_drying", AsyncMock())
    elig, sched = _patch_printer_manager({printer.id})
    with elig, sched:
        await dispatcher.check_queue()
    await db_session.refresh(first)
    await db_session.refresh(queue)
    if first_kind in {"due", "due_running"}:
        start.assert_not_awaited()
        assert first.waiting_reason_code == "storage_full"
        assert queue.is_paused
    else:
        storage.assert_not_called()
        start.assert_awaited_once()
        assert start.await_args.args[1].id == (first.id if first_kind == "later" else second.id)
        assert (
            first.waiting_reason_code == {"staged": "manual_start", "scheduled": "scheduled", "later": None}[first_kind]
        )
        assert not queue.is_paused


async def test_sql_python_occupancy_and_claim_agree_at_the_utc_boundary(db_session, printer_factory, clock):
    printer, _ = await _make_printer_with_queue(db_session, printer_factory)
    pending = [
        PrintQueueItem(queue_id=printer.id, position=1, scheduled_time=LATER),
        PrintQueueItem(queue_id=printer.id, position=2, manual_start=True),
        PrintQueueItem(queue_id=printer.id, position=3, scheduled_time=T.replace(tzinfo=None)),
        PrintQueueItem(queue_id=printer.id, position=4, scheduled_time=BEFORE),
        PrintQueueItem(queue_id=printer.id, position=5),
    ]
    done = PrintQueueItem(queue_id=printer.id, position=6, status="completed")
    db_session.add_all([*pending, done])
    await db_session.commit()
    clock.time = T
    rows = await printer_rows(db_session, printer.id)
    holding = {r.id for r in rows if printer_occupancy.row_holds_printer(r, T)}
    assert holding == {r.id for r in pending[2:]}
    assert holding == set(
        await db_session.scalars(select(PrintQueueItem.id).where(printer_occupancy.holding_condition(T)))
    )
    occupancy = await printer_occupancy.read_queue_occupancy(db_session, printer.id)
    assert set(occupancy.pending_item_ids) == {r.id for r in pending}
    assert set(occupancy.holding_item_ids) == holding
    with pytest.raises(printer_occupancy.PrinterOccupancyConflict, match="pending_backlog"):
        printer_occupancy.require_auto_placement(occupancy)
    assert await db_session.scalar(print_pass.first_runnable_query(printer.id, T, set())) == pending[2].id
    assert await db_session.scalar(print_pass.first_runnable_query(printer.id, T, {pending[2].id})) == pending[3].id
