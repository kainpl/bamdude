"""The background print-state observer and the shared reader behind Info, Health and support (spec r3 R5)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import dispatch_stall_tracker as tracker, print_state_recovery_store as store
from backend.app.services.print_run_binding import bind_print_run
from backend.app.services.print_state_observer import PrintStateObserver
from backend.app.services.printer_manager import printer_manager
from backend.tests.integration.test_printer_runtime_stall import (  # noqa: F401 - fixtures
    FakeClock,
    clock,
    runtime,
    stall_clock,
)
from backend.tests.unit.services.test_terminal_queue_repair import telemetry  # noqa: F401 - fixture

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def a_held_finished_run(db, printer_id: int) -> int:
    """A finished run whose context this process still holds; returns its archive id."""
    return (await a_held_run_and_queue(db, printer_id))[0]


async def a_held_run_and_queue(db, printer_id: int) -> tuple[int, int]:
    started = datetime.now(timezone.utc) - timedelta(hours=3)
    queue = PrinterQueue(printer_id=printer_id, status="idle")
    db.add(queue)
    await db.flush()
    archive = PrintArchive(
        printer_id=printer_id,
        queue_id=queue.id,
        filename="held.gcode.3mf",
        file_path="archives/held.gcode.3mf",
        file_size=1,
        status="completed",
        started_at=started,
        completed_at=started + timedelta(hours=1),
        extra_data={},
    )
    db.add(archive)
    await db.commit()
    bind_print_run(
        printer_manager, printer_id=printer_id, archive_id=archive.id, expected_submission_id="9", origin="dispatch"
    )
    return archive.id, queue.id


@pytest.fixture
def factory(test_engine):
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


async def test_a_stalled_dispatch_is_a_row_beside_the_context_that_holds_it(
    db_session, printer_factory, telemetry, runtime, clock, stall_clock
):
    """R3: dispatch_no_progress names the job, the attempts, and points at the related runtime row."""
    printer = await printer_factory()
    archive_id, queue_id = await a_held_run_and_queue(db_session, printer.id)
    pending = PrintQueueItem(queue_id=queue_id, status="pending", position=1)
    db_session.add(pending)
    await db_session.commit()
    telemetry.state = "FINISH"
    for _ in range(3):
        tracker.observe_deferral(printer.id, pending.id, "dispatch_claim_changed", "current_run_conflict", archive_id)
        stall_clock.advance(30)

    await store.list_issues(db_session, None)
    clock.advance(11)
    page = await store.list_issues(db_session, None)
    rows = {r["code"]: r for r in page["issues"] if r["printer_id"] == printer.id}
    stalled = rows["dispatch_no_progress"]
    assert stalled["kind"] == "episode" and stalled["item_id"] == pending.id and stalled["actions"] == []
    assert stalled["attempts"] == 3 and stalled["span_seconds"] == 60
    assert stalled["blocked_reasons"] == ["current_run_conflict"] and stalled["archive_id"] == archive_id
    assert stalled["related"] == {"kind": "runtime", "target_id": archive_id}
    assert "terminal_runtime_context" in rows


async def test_a_stall_that_went_quiet_is_inactive_not_resolved(
    db_session, factory, printer_factory, telemetry, runtime, clock, stall_clock
):
    """Minor 5 / R4: after a repair or a pause no new attempt comes. The row stays -- inactive, with how long it
    has been quiet -- and is not a problem Health counts; only progress or the job's end removes it."""
    printer = await printer_factory()
    archive_id, queue_id = await a_held_run_and_queue(db_session, printer.id)
    pending = PrintQueueItem(queue_id=queue_id, status="pending", position=1)
    db_session.add(pending)
    await db_session.commit()
    telemetry.state = "FINISH"
    for _ in range(3):
        tracker.observe_deferral(printer.id, pending.id, "dispatch_claim_changed", "current_run_conflict", archive_id)
        stall_clock.advance(30)
    stall_clock.advance(tracker.WINDOW_SECONDS + 1)

    observer = PrintStateObserver(session_factory=factory)
    await observer.tick()
    clock.advance(11)
    await observer.tick()
    page = await store.list_issues(db_session, None)
    (row,) = [r for r in page["issues"] if r["code"] == "dispatch_no_progress"]
    assert row["active"] is False and row["observation"] is True
    assert row["quiet_seconds"] == tracker.WINDOW_SECONDS + 31
    summary = observer.summary(None)
    assert "dispatch_no_progress" not in summary["by_code"]
    assert summary["observations"]["dispatch_no_progress"] == 1


async def test_an_episode_links_only_to_a_row_that_is_shown(
    db_session, printer_factory, telemetry, runtime, clock, stall_clock
):
    """Minor 5: the held context is not on the page yet (seen once only) -- no link that leads nowhere."""
    printer = await printer_factory()
    archive_id, queue_id = await a_held_run_and_queue(db_session, printer.id)
    pending = PrintQueueItem(queue_id=queue_id, status="pending", position=1)
    db_session.add(pending)
    await db_session.commit()
    telemetry.state = "FINISH"
    for _ in range(3):
        tracker.observe_deferral(printer.id, pending.id, "dispatch_claim_changed", "current_run_conflict", archive_id)
        stall_clock.advance(30)
    page = await store.list_issues(db_session, None)
    (row,) = [r for r in page["issues"] if r["code"] == "dispatch_no_progress"]
    assert not [r for r in page["issues"] if r["kind"] == "runtime"]
    assert row["related"] is None


async def test_what_the_stall_tracking_covers_is_said(
    db_session, factory, printer_factory, telemetry, runtime, clock, stall_clock, monkeypatch
):
    """Minor 5 / R4: since when stalls are tracked, and whether any record was dropped, reach Info and Health."""
    await printer_factory()
    stall_clock.advance(120)
    monkeypatch.setattr(tracker, "_evicted", 3)
    page = await store.list_issues(db_session, None)
    assert page["stall_tracking"] == {"since_seconds": 120, "evicted": 3}
    observer = PrintStateObserver(session_factory=factory)
    await observer.tick()
    assert observer.public_summary(None)["stall_tracking"] == {"since_seconds": 120, "evicted": 3}


async def test_an_episode_of_a_job_that_is_gone_is_not_a_row(
    db_session, printer_factory, telemetry, runtime, stall_clock
):
    printer = await printer_factory()
    db_session.add(PrinterQueue(printer_id=printer.id, status="idle"))
    await db_session.commit()
    for _ in range(3):
        tracker.observe_deferral(printer.id, 999_999, "dispatch_claim_changed", "claim_missing", None)
        stall_clock.advance(30)
    page = await store.list_issues(db_session, None)
    assert not [r for r in page["issues"] if r["code"] == "dispatch_no_progress"]


async def test_one_tick_reads_one_page_and_keeps_no_session(
    db_session, factory, printer_factory, telemetry, runtime, clock, monkeypatch
):
    printers = [await printer_factory(name=f"p{n}") for n in range(23)]
    for p in printers:
        await a_held_finished_run(db_session, p.id)
    telemetry.state = "FINISH"
    open_sessions = []

    class Counting:
        def __call__(self):
            session = factory()
            open_sessions.append(session)
            return session

    observer = PrintStateObserver(session_factory=Counting())
    await observer.tick()
    assert observer.summary(None)["coverage"]["cycle_complete"] is False
    assert len(observer.summary(None)["coverage"]["printer_ids"]) == 20
    clock.advance(11)
    await observer.tick()  # the next page: the three remaining printers, and the cycle wraps
    assert observer.summary(None)["coverage"]["cycle_complete"] is True
    assert all(not s.in_transaction() for s in open_sessions)


async def test_the_observer_counts_what_the_panel_shows(
    db_session, factory, printer_factory, telemetry, runtime, clock
):
    printer = await printer_factory()
    await a_held_finished_run(db_session, printer.id)
    telemetry.state = "FINISH"
    observer = PrintStateObserver(session_factory=factory)
    await observer.tick()
    clock.advance(11)
    await observer.tick()
    summary = observer.summary(None)
    assert summary["by_code"] == {"terminal_runtime_context": 1}
    assert summary["printers"] == [{"printer_id": printer.id, "codes": ["terminal_runtime_context"]}]
    assert summary["checked_at"] is not None and summary["error"] is None


@pytest.mark.parametrize("summary_in_between", [True, False])
async def test_a_page_read_before_a_restore_is_never_published(monkeypatch, summary_in_between):
    """RS-R4: the read began on the old database; whatever happened to the cache meanwhile, its rows are dropped."""
    from backend.app.services import print_state_observer as module

    epoch = [0]
    monkeypatch.setattr(module.queue_sources, "current_epoch", lambda: epoch[0])

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    observer = PrintStateObserver(session_factory=Session)

    async def old_page(*_args, **_kwargs):
        epoch[0] = 1  # a restore lands while this page is being read
        if summary_in_between:
            observer.summary(None)  # a Health request in between already reset the cache
        return {"issues": [{"printer_id": 123, "code": "terminal_runtime_context"}], "next_cursor": None}, [123]

    monkeypatch.setattr(store, "list_issues_page", old_page)
    await observer.tick()
    summary = observer.summary(None)
    assert summary["printers"] == [] and summary["coverage"]["printer_ids"] == []
    assert summary["coverage"]["cycle_complete"] is False


async def test_a_page_cut_short_marks_only_the_printers_it_read(
    db_session, factory, printer_factory, telemetry, runtime, clock
):
    """Minor 8: rows past the page's cut belong to the next page -- their printers are not "checked" yet."""
    first = await printer_factory(name="busy")
    second = await printer_factory(name="after the cut")
    started = datetime.now(timezone.utc) - timedelta(hours=1)
    db_session.add_all(
        PrintArchive(
            printer_id=first.id,
            filename=f"part-{n}.gcode.3mf",
            file_path=f"archives/part-{n}.gcode.3mf",
            file_size=1,
            status="printing",
            started_at=started,
            extra_data={},
        )
        for n in range(101)
    )
    await db_session.commit()
    observer = PrintStateObserver(session_factory=factory)
    await observer.tick()
    assert second.id not in observer.summary(None)["coverage"]["printer_ids"]
    await observer.tick()
    assert {first.id, second.id} <= set(observer.summary(None)["coverage"]["printer_ids"])


async def test_an_observation_is_shown_but_never_counted_as_a_problem(
    db_session, factory, printer_factory, telemetry, runtime, clock
):
    """A completion past its budget is listed in Info as an observation; Health counts problems only."""
    from backend.app.services.print_run_binding import begin_print_run_finishing

    printer = await printer_factory()
    bind_print_run(printer_manager, printer_id=printer.id, archive_id=77, origin="dispatch")
    begin_print_run_finishing(printer_manager, printer.id, 77)
    telemetry.state = "FINISH"
    observer = PrintStateObserver(session_factory=factory)
    await observer.tick()
    clock.advance(store.LINGERING_SECONDS + 1)
    await observer.tick()
    page = await store.list_issues(db_session, None)
    assert [r["code"] for r in page["issues"] if r["printer_id"] == printer.id] == ["runtime_phase_lingering"]
    summary = observer.summary(None)
    assert summary["by_code"] == {} and summary["printers"] == []
    assert summary["observations"] == {"runtime_phase_lingering": 1}


async def test_the_summary_follows_the_callers_printer_scope(
    db_session, factory, printer_factory, telemetry, runtime, clock
):
    first, second = await printer_factory(name="a"), await printer_factory(name="b")
    await a_held_finished_run(db_session, first.id)
    await a_held_finished_run(db_session, second.id)
    telemetry.state = "FINISH"
    observer = PrintStateObserver(session_factory=factory)
    await observer.tick()
    clock.advance(11)
    await observer.tick()
    assert observer.summary(frozenset({second.id}))["by_code"] == {"terminal_runtime_context": 1}
    assert [p["printer_id"] for p in observer.summary(frozenset({second.id}))["printers"]] == [second.id]
    assert observer.summary(frozenset())["by_code"] == {}


async def test_a_deleted_printer_leaves_the_cache_after_a_full_cycle(
    db_session, factory, printer_factory, telemetry, runtime, clock
):
    keep, gone = await printer_factory(name="keep"), await printer_factory(name="gone")
    await a_held_finished_run(db_session, keep.id)
    await a_held_finished_run(db_session, gone.id)
    telemetry.state = "FINISH"
    observer = PrintStateObserver(session_factory=factory)
    await observer.tick()
    clock.advance(11)
    await observer.tick()
    await db_session.delete(gone)
    await db_session.commit()
    await observer.tick()
    assert [p["printer_id"] for p in observer.summary(None)["printers"]] == [keep.id]


async def test_a_restore_empties_the_cache(db_session, factory, printer_factory, telemetry, runtime, clock):
    from backend.app.services import queue_sources

    printer = await printer_factory()
    await a_held_finished_run(db_session, printer.id)
    telemetry.state = "FINISH"
    observer = PrintStateObserver(session_factory=factory)
    await observer.tick()
    clock.advance(11)
    await observer.tick()
    queue_sources.invalidate_receipts()
    assert observer.summary(None)["printers"] == []
    assert observer.summary(None)["coverage"]["cycle_complete"] is False


async def test_a_failing_read_is_an_error_not_a_healthy_farm(factory, monkeypatch):
    observer = PrintStateObserver(session_factory=factory)

    async def broken(*_args, **_kwargs):
        raise RuntimeError("database gone")

    monkeypatch.setattr(store, "list_issues_page", broken)
    await observer.tick()
    summary = observer.summary(None)
    assert summary["error"] == "unavailable" and summary["by_code"] == {}
    assert summary["coverage"]["cycle_complete"] is False


@pytest.mark.parametrize("count", [3, 18])
async def test_a_tick_costs_the_same_queries_whatever_the_farm_and_its_history(
    db_session, factory, test_engine, printer_factory, archive_factory, telemetry, runtime, clock, count
):
    """A14: no N+1 over printers, no scan of terminal history."""
    for n in range(count):
        p = await printer_factory(name=f"p{n}")
        await a_held_finished_run(db_session, p.id)
        for _ in range(5):
            await archive_factory(p.id, status="completed")
    telemetry.state = "FINISH"
    statements = []

    def count_statement(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", count_statement)
    try:
        await PrintStateObserver(session_factory=factory).tick()
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", count_statement)
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    # Fixed batch reads, including plate-gate owners and recovery action intent/outcome rows.
    # Both farm sizes fit the same budget; per-printer or terminal-history reads exceed it.
    assert len(selects) <= 16, len(selects)


async def test_the_loop_survives_a_failing_tick_and_stops_cleanly(factory, monkeypatch):
    observer = PrintStateObserver(session_factory=factory, interval=0.01)
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("one bad tick")

    monkeypatch.setattr(observer, "tick", flaky)
    observer.start()
    for _ in range(200):
        if calls >= 3:
            break
        await asyncio.sleep(0.01)
    await observer.stop()
    assert calls >= 3
    assert observer._task is None


async def filled_observer(db_session, printer_factory, telemetry, clock, monkeypatch):
    """The process-wide observer after two ticks over one held finished run."""
    from backend.app.services import print_state_observer as module

    printer = await printer_factory()
    await a_held_finished_run(db_session, printer.id)
    telemetry.state = "FINISH"
    observer = PrintStateObserver()
    monkeypatch.setattr(module, "print_state_observer", observer)
    await observer.tick()
    clock.advance(11)
    await observer.tick()
    return printer, observer


async def test_system_health_mentions_print_state_problems(
    async_client, db_session, printer_factory, telemetry, runtime, clock, monkeypatch
):
    printer, _ = await filled_observer(db_session, printer_factory, telemetry, clock, monkeypatch)
    response = await async_client.get("/api/v1/system/health")
    assert response.status_code == 200, response.text
    state = response.json()["print_state"]
    assert state["by_code"] == {"terminal_runtime_context": 1}
    assert state["printers"] == [{"printer_id": printer.id, "codes": ["terminal_runtime_context"]}]
    assert state["coverage"]["cycle_complete"] is True


async def test_system_health_counts_only_the_keys_printers(
    async_client, db_session, printer_factory, telemetry, runtime, clock, monkeypatch
):
    await filled_observer(db_session, printer_factory, telemetry, clock, monkeypatch)
    made = await async_client.post(
        "/api/v1/api-keys/", json={"name": "scoped", "printer_ids": [], "can_read_status": True}
    )
    assert made.status_code == 200, made.text
    async_client.headers.pop("Authorization", None)
    response = await async_client.get("/api/v1/system/health", headers={"X-API-Key": made.json()["key"]})
    assert response.status_code == 200, response.text
    state = response.json()["print_state"]
    assert state is None or (state["by_code"] == {} and state["printers"] == [])


async def test_the_support_bundle_asks_the_print_state_read_rights(db_session, monkeypatch):
    """Minor 11: the bundle's print-state section is read with the very gates Info and Health ask."""
    from backend.app.api.routes import support
    from backend.app.api.routes.print_state_recovery import READ
    from backend.app.core.auth import RequestCredentials
    from backend.app.services import diagnostic_snapshot

    def denying(denied):
        class Creds(RequestCredentials):
            async def allows(self, gate):
                return gate is not denied

        return Creds(None, None, None)

    for gate in READ:
        assert await support._print_state_access(None, denying(gate)) == (False, None)
    assert await support._print_state_access(None, denying(None)) == (True, None)
    assert await support._print_state_access(None, None) == (False, None)  # no request credentials: nobody

    monkeypatch.setattr(diagnostic_snapshot, "_run_connection_for", lambda p: asyncio.sleep(0, result={}))
    monkeypatch.setattr(diagnostic_snapshot, "_run_log_health", lambda: asyncio.sleep(0, result={}))
    snapshot = await diagnostic_snapshot.collect_diagnostic_snapshot(db_session, include_print_state=False)
    assert snapshot["print_state"] == {"omitted": "permissions"}


async def test_the_support_snapshot_carries_the_same_bounded_reading(
    db_session, printer_factory, telemetry, runtime, clock, monkeypatch
):
    from backend.app.services import diagnostic_snapshot

    printer, observer = await filled_observer(db_session, printer_factory, telemetry, clock, monkeypatch)
    monkeypatch.setattr(diagnostic_snapshot, "_run_connection_for", lambda p: asyncio.sleep(0, result={}))
    monkeypatch.setattr(diagnostic_snapshot, "_run_log_health", lambda: asyncio.sleep(0, result={}))
    snapshot = await diagnostic_snapshot.collect_diagnostic_snapshot(db_session)
    section = snapshot["print_state"]
    assert section["by_code"] == {"terminal_runtime_context": 1}
    assert section["printers"] == [{"printer_id": printer.id, "codes": ["terminal_runtime_context"]}]
    assert "printer_ids" not in section["coverage"]
    scoped = await diagnostic_snapshot.collect_diagnostic_snapshot(db_session, print_state_scope=frozenset())
    assert scoped["print_state"]["by_code"] == {}
