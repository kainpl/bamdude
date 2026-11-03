"""A printer whose finished run is still held in memory, and the dispatch loop it causes.

Spec: vault ``60-specs/printer-runtime-stall-diagnostics-spec.md`` (r3). The paths here are the
production ones -- the connect-edge sweep, the bound-terminal acceptance, the real scheduler and
runner with device I/O faked -- and every printer observation is synthetic. These fixtures build
the broken state on purpose; they do not claim to be the cause of any user's log.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import print_state_recovery_store as store
from backend.app.services.print_run_binding import (
    begin_print_run_finishing,
    bind_prepared_print_run,
    bind_print_run,
    current_print_run,
)
from backend.app.services.printer_manager import printer_manager
from backend.tests.integration.test_dispatch_attempt_verdicts import a_job_and_its_dispatch, one_tick
from backend.tests.integration.test_dispatch_without_original import (  # noqa: F401 - fixtures
    clean_spool_state,
    sessions,
)
from backend.tests.unit.services.test_terminal_queue_repair import telemetry  # noqa: F401 - fixture

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

_RUNTIME_REGISTRIES = (
    "_print_run_bindings",
    "_print_run_finishing_bindings",
    "_print_start_resolutions",
    "_pending_print_terminals",
    "_print_start_admissions",
    "_print_effect_generations",
    "_print_terminal_generations",
)


class FakeClock:
    """The monotonic clock the diagnostics read, moved by the test alone."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(store, "clock", fake)
    monkeypatch.setattr(store, "_runtime_observations", {})
    monkeypatch.setattr(store, "_observations", {})
    return fake


async def listed(db, clock, printer_id, *, code="terminal_runtime_context"):
    """Two equal observations at least ten seconds apart -- what the panel needs before it shows a row."""
    first = await store.list_issues(db, None)
    assert not [r for r in first["issues"] if r["printer_id"] == printer_id and r["kind"] == "runtime"], first
    clock.advance(11)
    page = await store.list_issues(db, None)
    return next((r for r in page["issues"] if r["printer_id"] == printer_id and r["code"] == code), None), page


@pytest.fixture
def runtime(monkeypatch):
    """The empty in-process context a fresh server has, for this test only."""
    from backend.app import main

    for name in _RUNTIME_REGISTRIES:
        monkeypatch.setattr(printer_manager, name, {}, raising=False)
    # The plate gate's in-memory mirror is process-wide too, and printer ids repeat between tests.
    monkeypatch.setattr(printer_manager, "_awaiting_plate_clear", set())
    monkeypatch.setattr(main, "_active_swap_config", {})
    monkeypatch.setattr(main, "_active_macro_selection", {})
    return printer_manager


async def a_dispatched_run_the_sweep_closes(db, printer_factory, archive_factory, telemetry):
    """A queued print A whose finish was missed while the link was down, closed by the connect-edge sweep.

    Everything is the production path: the dispatch's own binding and macro registration, then
    ``_reconcile`` against a printer that reports FINISH for A's file.
    """
    from backend.app import main
    from backend.app.services.print_reconciliation import _reconcile

    printer = await printer_factory()
    queue = PrinterQueue(printer_id=printer.id, status="printing")
    db.add(queue)
    await db.flush()
    started = datetime.now(timezone.utc) - timedelta(hours=2)
    archive = await archive_factory(
        printer.id,
        queue_id=queue.id,
        filename="cube.gcode.3mf",
        status="printing",
        started_at=started,
        completed_at=None,
        created_at=started,
        extra_data={},
    )
    item = PrintQueueItem(queue_id=queue.id, archive_id=archive.id, status="printing", started_at=started)
    pending = PrintQueueItem(queue_id=queue.id, status="pending", position=1)
    db.add_all([item, pending])
    await db.flush()
    queue.current_item_id = item.id
    await db.commit()

    run = bind_prepared_print_run(
        printer_manager,
        printer_id=printer.id,
        archive_id=archive.id,
        queue_item_id=item.id,
        claim_started_at=started,
        expected_submission_id="111",
    )
    assert run is not None
    # What every Print dialog dispatch registers after its start: an empty selection is a real answer.
    main.register_macro_selection(printer.id, {"selected_macro_ids": []})

    telemetry.state = "FINISH"
    closed = await _reconcile(db, printer.id, "FINISH", "cube.gcode.3mf")
    await db.commit()
    assert closed == [archive.id]
    return printer, queue, archive, item, pending, run


async def test_a_reconciled_run_whose_context_stayed_is_shown(
    db_session, test_engine, printer_factory, archive_factory, telemetry, runtime, clock, monkeypatch
):
    """A01: the sweep closes A in the database and leaves A bound in memory; the diagnostics say so."""
    from backend.app import main

    monkeypatch.setattr(
        main, "async_session", async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    )
    printer, queue, archive, item, pending, run = await a_dispatched_run_the_sweep_closes(
        db_session, printer_factory, archive_factory, telemetry
    )
    printer_id, queue_id, archive_id, item_id, pending_id = printer.id, queue.id, archive.id, item.id, pending.id

    db_session.expire_all()
    assert (await db_session.get(PrintArchive, archive_id)).status == "completed"
    closed_queue = await db_session.get(PrinterQueue, queue_id)
    assert closed_queue.status == "idle" and not closed_queue.is_paused and closed_queue.current_item_id is None
    assert (await db_session.get(PrintQueueItem, pending_id)).status == "pending"
    # The sweep never touched the in-process context.
    assert current_print_run(printer_manager, printer_id) is run
    # A late terminal for A cannot release it: the archive is no longer printing.
    assert await main._accept_bound_terminal_run(printer_id, run, {"status": "completed"}) is None
    assert current_print_run(printer_manager, printer_id) is run
    # And every next attempt on this printer is refused at its bind.
    assert (
        bind_prepared_print_run(
            printer_manager, printer_id=printer_id, archive_id=archive_id + 1000, queue_item_id=pending_id
        )
        is None
    )

    issue, page = await listed(db_session, clock, printer_id)
    assert issue is not None, page["issues"]
    assert issue["archive_id"] == archive_id and issue["kind"] == "runtime" and issue["target_id"] == archive_id
    assert issue["queue_id"] == queue_id and issue["item_id"] is None
    # The empty selection every Print dialog registers is no veto (R7a); nothing else holds A.
    assert issue["actions"] == ["clear_terminal_runtime_context"], issue["blocked_reasons"]
    assert issue["context"] == {"archive_id": archive_id, "queue_item_id": item_id, "origin": "dispatch"}
    assert issue["queue_paused"] is False


async def test_a_row_seen_on_every_read_keeps_its_whole_age(db_session, printer_factory, telemetry, runtime, clock):
    """A context still seen is never forgotten mid-way: no read hides it, and its age keeps growing."""
    printer = await printer_factory()
    await a_stale_context_on(db_session, printer.id)
    telemetry.state = "FINISH"
    issue, _page = await listed(db_session, clock, printer.id)
    assert issue is not None and issue["age_seconds"] == 11
    age = 11
    for step in (60, 120, 120, 15, 300):
        clock.advance(step)
        age += step
        page = await store.list_issues(db_session, None)
        rows = [r for r in page["issues"] if r["printer_id"] == printer.id and r["kind"] == "runtime"]
        assert [r["age_seconds"] for r in rows] == [age], (age, rows)


async def test_a_completion_still_finishing_is_shown_only_past_its_budget(
    db_session, printer_factory, telemetry, runtime, clock
):
    """A live completion's finishing lease lasts as long as its effects (photo, cleanup, timelapse): that is an
    ordinary phase. Only one that outlives the completion budget is shown -- as an observation with its age."""
    printer = await printer_factory()
    bind_print_run(printer_manager, printer_id=printer.id, archive_id=77, origin="dispatch")
    begin_print_run_finishing(printer_manager, printer.id, 77)
    telemetry.state = "FINISH"

    def rows(page):
        return [r for r in page["issues"] if r["printer_id"] == printer.id and r["kind"] == "runtime"]

    assert rows(await store.list_issues(db_session, None)) == []
    clock.advance(store.LINGERING_SECONDS - 1)
    assert rows(await store.list_issues(db_session, None)) == []
    clock.advance(2)
    (row,) = rows(await store.list_issues(db_session, None))
    assert row["code"] == "runtime_phase_lingering" and row["observation"] is True
    assert row["blocked_reasons"] == ["finishing_in_progress"] and row["actions"] == []
    assert row["age_seconds"] == store.LINGERING_SECONDS + 1


def test_an_observation_is_forgotten_only_after_it_stops_being_seen(clock):
    table: dict = {}
    assert store._seen_for(table, ("a",)) == 0
    for _ in range(4):
        clock.advance(200)
        store._seen_for(table, ("a",))
    assert store._seen_for(table, ("a",)) == 800
    clock.advance(store.OBSERVATION_HORIZON_SECONDS + 1)
    assert store._seen_for(table, ("b",)) == 0
    assert ("a",) not in table  # unseen past the horizon: dropped
    assert store._seen_for(table, ("a",)) == 0


async def test_reading_the_runtime_context_changes_nothing(
    db_session, test_engine, printer_factory, archive_factory, telemetry, runtime, clock, monkeypatch
):
    """GET is a read: no registry, no row, no reconnect path (spec r3 R1, R2)."""
    from copy import copy

    from backend.app import main

    monkeypatch.setattr(
        main, "async_session", async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    )
    printer, *_ = await a_dispatched_run_the_sweep_closes(db_session, printer_factory, archive_factory, telemetry)

    def registries():
        state = {name: copy(getattr(printer_manager, name, {})) for name in _RUNTIME_REGISTRIES}
        state["macro"] = copy(main._active_macro_selection)
        state["swap"] = copy(main._active_swap_config)
        return state

    before = registries()
    monkeypatch.setattr(
        printer_manager, "get_status", lambda *_: (_ for _ in ()).throw(AssertionError("reconnect path"))
    )
    await listed(db_session, clock, printer.id)
    assert registries() == before
    assert not db_session.new and not db_session.dirty and not db_session.deleted


async def test_a_printer_without_a_queue_still_has_its_row(
    db_session, printer_factory, archive_factory, telemetry, runtime, clock
):
    """A runtime-only context is shown, but nothing can be repaired without a real queue (R7.3)."""
    printer = await printer_factory()
    archive = await a_stale_context_on(db_session, printer.id)
    telemetry.state = "FINISH"

    issue, page = await listed(db_session, clock, printer.id)
    assert issue is not None, page["issues"]
    assert issue["queue_id"] is None and issue["actions"] == []
    assert "queue_missing" in issue["blocked_reasons"]
    assert issue["archive_id"] == archive.id


async def test_runtime_rows_page_with_their_printers(db_session, printer_factory, telemetry, runtime, clock):
    """More printers than one page: every runtime row is seen exactly once (A14)."""
    printers = [await printer_factory(name=f"p{n}") for n in range(23)]
    for p in printers:
        await a_stale_context_on(db_session, p.id)
    telemetry.state = "FINISH"

    async def walk():
        seen, cursor = [], None
        for _ in range(10):
            page = await store.list_issues(db_session, None, cursor=cursor)
            seen.extend((r["printer_id"], r["kind"], r["target_id"]) for r in page["issues"] if r["kind"] == "runtime")
            cursor = page["next_cursor"]
            if not cursor:
                return seen
        raise AssertionError("the walk did not end")

    await walk()
    clock.advance(11)
    seen = await walk()
    assert len(seen) == len(set(seen)) == 23
    assert {pid for pid, _, _ in seen} == {p.id for p in printers}


async def a_stale_context_on(db, printer_id: int) -> PrintArchive:
    """A finished run A on this printer that stayed bound in memory."""
    started = datetime.now(timezone.utc) - timedelta(hours=3)
    stale = PrintArchive(
        printer_id=printer_id,
        filename="stale.gcode.3mf",
        file_path="archives/stale.gcode.3mf",
        file_size=1,
        status="completed",
        started_at=started,
        completed_at=started + timedelta(hours=1),
        extra_data={},
    )
    db.add(stale)
    await db.commit()
    bind_print_run(
        printer_manager, printer_id=printer_id, archive_id=stale.id, expected_submission_id="111", origin="dispatch"
    )
    return stale


async def test_every_stalled_attempt_is_a_new_job_and_archive_behind_one_blocker(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions, runtime
):
    """Late race: a context appears during each upload, after the early readiness check."""
    queue_id, mqtt, item_id, _service, scheduler, spawned = await a_job_and_its_dispatch(
        db_session, test_engine, tmp_path, printer_factory, monkeypatch
    )
    printer_id = (await db_session.get(PrinterQueue, queue_id)).printer_id
    stale_id = (await a_stale_context_on(db_session, printer_id)).id

    for _ in range(3):
        context_arrives_during_upload(monkeypatch, printer_id)
        await one_tick(scheduler, spawned)
        db_session.expire_all()
        row = await db_session.get(PrintQueueItem, item_id)
        assert row.status == "pending" and row.started_at is None, row.error_message

    db_session.expire_all()
    attempts = (
        await db_session.execute(
            select(PrintArchive.id, PrintArchive.status, PrintArchive.extra_data).where(
                PrintArchive.printer_id == printer_id, PrintArchive.status != "completed"
            )
        )
    ).all()
    assert len({a.id for a in attempts}) == 3
    assert all(a.status == "cancelled" and a.extra_data.get("dispatch_aborted") for a in attempts)
    mqtt._client.publish.assert_not_called()
    assert current_print_run(printer_manager, printer_id).archive_id == stale_id


def context_arrives_during_upload(monkeypatch, printer_id):
    """Expose the existing synthetic context only at the upload barrier.

    A known context is now an early wait. These tests deliberately exercise the
    distinct late refusal and its existing tracker, without disabling that check.
    """
    import backend.app.services.background_dispatch as bd

    held = printer_manager._print_run_bindings.pop(printer_id)

    async def upload(*_args, **_kwargs):
        printer_manager._print_run_bindings[printer_id] = held
        return True

    monkeypatch.setattr(bd, "upload_file_async", AsyncMock(side_effect=upload))


@pytest.fixture
def stall_clock(monkeypatch):
    from backend.app.services import dispatch_stall_tracker as tracker

    fake = FakeClock()
    monkeypatch.setattr(tracker, "clock", fake)
    tracker.reset()
    yield fake
    tracker.reset()


@pytest.mark.parametrize("kind", ["library", "archive"])
async def test_a_stalled_attempt_names_its_printer_item_and_blocker(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions, runtime, stall_clock, caplog, kind
):
    """R4: the deferral says which printer and job it was, and what held it -- never a guess. Both runners."""
    queue_id, _mqtt, item_id, _service, scheduler, spawned = await a_job_and_its_dispatch(
        db_session, test_engine, tmp_path, printer_factory, monkeypatch, kind
    )
    printer_id = (await db_session.get(PrinterQueue, queue_id)).printer_id
    stale_id = (await a_stale_context_on(db_session, printer_id)).id

    context_arrives_during_upload(monkeypatch, printer_id)
    with caplog.at_level(logging.INFO, logger="backend.app.services.background_dispatch"):
        await one_tick(scheduler, spawned)

    deferrals = [r.getMessage() for r in caplog.records if "deferred before publish" in r.getMessage()]
    assert deferrals, [r.getMessage() for r in caplog.records]
    line = deferrals[-1]
    assert f"printer={printer_id}" in line and f"item={item_id}" in line, line
    assert "detail=current_run_conflict" in line and f"blocker_archive={stale_id}" in line, line


async def test_the_real_loop_becomes_one_stalled_episode(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions, runtime, stall_clock, caplog
):
    """A11: every attempt is a new job and archive, the episode is one -- and one WARNING says so."""
    from backend.app.services import dispatch_stall_tracker as tracker

    queue_id, _mqtt, item_id, _service, scheduler, spawned = await a_job_and_its_dispatch(
        db_session, test_engine, tmp_path, printer_factory, monkeypatch
    )
    printer_id = (await db_session.get(PrinterQueue, queue_id)).printer_id
    stale_id = (await a_stale_context_on(db_session, printer_id)).id

    with caplog.at_level(logging.WARNING, logger=tracker.__name__):
        for _ in range(4):
            context_arrives_during_upload(monkeypatch, printer_id)
            await one_tick(scheduler, spawned)
            stall_clock.advance(40)
    (episode,) = tracker.snapshot(printer_id)
    assert (episode.item_id, episode.detail, episode.blocker_archive_id) == (item_id, "current_run_conflict", stale_id)
    assert episode.count == 4 and episode.stalled
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and f"printer={printer_id}" in warnings[0]


async def a_claimed_item(db, printer_factory):
    from backend.app.services.background_dispatch import PrintDispatchJob

    printer = await printer_factory()
    queue = PrinterQueue(printer_id=printer.id, status="printing")
    db.add(queue)
    await db.flush()
    item = PrintQueueItem(
        queue_id=queue.id,
        status="printing",
        started_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        auto_eject=False,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    job = PrintDispatchJob(
        id=1,
        kind="print_library_file",
        source_id=None,
        source_name="part.3mf",
        printer_id=printer.id,
        printer_name=printer.name,
        queue_item_id=item.id,
        claim_started_at=item.started_at,
        routing_intent=item.filament_routing,
        options={"auto_eject": False, "auto_eject_settings": None},
    )
    return printer, queue, item, job


@pytest.mark.parametrize(
    ("change", "detail"),
    [
        ("delete", "claim_missing"),
        ("status", "claim_released"),
        ("started_at", "claim_restarted"),
        ("routing", "routing_changed"),
        ("auto_eject", "auto_eject_changed"),
        ("auto_eject_settings", "auto_eject_changed"),
    ],
)
async def test_the_final_claim_check_names_what_changed(db_session, printer_factory, change, detail):
    """R4: every branch of ``_verify_routing_claim`` has its own detail; the public code stays the same."""
    from backend.app.services.background_dispatch import BackgroundDispatchService
    from backend.app.services.filament_routing import RoutingDeferred

    _printer, _queue, item, job = await a_claimed_item(db_session, printer_factory)
    if change == "delete":
        await db_session.delete(item)
    elif change == "status":
        item.status = "pending"
    elif change == "started_at":
        item.started_at = item.started_at + timedelta(seconds=5)
    elif change == "routing":
        item.filament_routing = '{"mode": "pinned"}'
    elif change == "auto_eject":
        item.auto_eject = True
    else:
        item.auto_eject_settings = {"lift": 5}
    await db_session.commit()

    with pytest.raises(RoutingDeferred) as raised:
        await BackgroundDispatchService()._verify_routing_claim(db_session, job)
    assert raised.value.reason == "dispatch_claim_changed"
    assert raised.value.detail == detail


@pytest.mark.parametrize(("change", "detail"), [("delete", "claim_missing"), ("status", "claim_released")])
async def test_a_claim_lost_before_routing_is_named(db_session, printer_factory, change, detail):
    from contextlib import AsyncExitStack

    from backend.app.services.background_dispatch import BackgroundDispatchService
    from backend.app.services.filament_routing import RoutingDeferred

    _printer, _queue, item, job = await a_claimed_item(db_session, printer_factory)
    if change == "delete":
        await db_session.delete(item)
    else:
        item.status = "pending"
    await db_session.commit()

    async with AsyncExitStack() as pins:
        with pytest.raises(RoutingDeferred) as raised:
            await BackgroundDispatchService()._prepare_filament_routing(db_session, job, pins)
    assert (raised.value.reason, raised.value.detail) == ("dispatch_claim_changed", detail)


async def test_a_job_repointed_during_its_copy_is_named(
    db_session, test_engine, printer_factory, archive_factory, monkeypatch
):
    """R4: the source link moved while the descriptor-less copy ran."""
    from contextlib import AsyncExitStack
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from backend.app.services import queue_source_capture
    from backend.app.services.background_dispatch import BackgroundDispatchService
    from backend.app.services.filament_routing import RoutingDeferred

    printer, _queue, item, job = await a_claimed_item(db_session, printer_factory)
    source = await archive_factory(printer.id, status="completed")
    other = await archive_factory(printer.id, status="completed")
    item.archive_id = source.id
    await db_session.commit()
    job.kind, job.source_id = "reprint_archive", source.id
    job.original_archive_id, job.original_library_file_id = source.id, None
    other_id, item_id = other.id, item.id
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    async def copy_while_the_job_moves(*_args, **_kwargs):
        async with factory() as elsewhere:
            row = await elsewhere.get(PrintQueueItem, item_id)
            row.archive_id = other_id
            await elsewhere.commit()
        return SimpleNamespace(descriptor=object())

    monkeypatch.setattr(queue_source_capture, "plan_capture", lambda **_: object())
    monkeypatch.setattr(queue_source_capture, "capture_for_dispatch", copy_while_the_job_moves)
    monkeypatch.setattr(queue_source_capture, "discard_staged", AsyncMock())

    async with AsyncExitStack() as pins:
        with pytest.raises(RoutingDeferred) as raised:
            await BackgroundDispatchService()._capture_descriptorless_source(db_session, job, pins)
    assert (raised.value.reason, raised.value.detail) == ("dispatch_claim_changed", "source_link_changed")


@pytest.mark.parametrize("how", ["single", "block"])
async def test_a_retry_starts_a_new_episode(db_session, printer_factory, stall_clock, how):
    """R4: the operator's Retry is a new incarnation of the job, so its failures are counted afresh."""
    from backend.app.services import dispatch_stall_tracker as tracker
    from backend.tests.integration.test_dispatch_attempt_verdicts import _retry

    printer, queue, item, _job = await a_claimed_item(db_session, printer_factory)
    for _ in range(2):
        tracker.observe_deferral(printer.id, item.id, "dispatch_claim_changed", "current_run_conflict", 19)
        stall_clock.advance(30)
    item.status = "failed"
    queue.status = "idle"
    await db_session.commit()

    await _retry(db_session, queue.id, item.id, how)
    tracker.observe_deferral(printer.id, item.id, "dispatch_claim_changed", "current_run_conflict", 19)

    episodes = tracker.snapshot(printer.id)
    assert len(episodes) == 2 and not any(e.stalled for e in episodes)
