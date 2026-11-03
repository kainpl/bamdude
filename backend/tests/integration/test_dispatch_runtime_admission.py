"""A known run vetoes preparation; a later run still owns the final bind (GH109 r2)."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.models.print_queue import PrintQueueItem
from backend.app.services.background_dispatch import BackgroundDispatchService, DispatchEnqueueRejected
from backend.app.services.print_run_binding import (
    begin_print_run_finishing,
    bind_prepared_print_run,
    bind_print_run,
    current_print_run,
    finishing_print_runs,
    prepared_print_run_conflicts,
)
from backend.app.services.printer_manager import printer_manager
from backend.tests.integration.test_auto_queue_scheduler import (
    _make_printer_with_queue,
    _patch_printer_manager,
    routing_item,  # noqa: F401
    scheduler as auto_scheduler,  # noqa: F401
)
from backend.tests.integration.test_dispatch_attempt_verdicts import a_job_and_its_dispatch, one_tick
from backend.tests.integration.test_dispatch_without_original import clean_spool_state, sessions  # noqa: F401
from backend.tests.integration.test_printer_runtime_stall import a_claimed_item, runtime, stall_clock  # noqa: F401
from backend.tests.integration.test_runtime_recovery_acceptance import (
    a_finished_run_held_over_a_pending_job,
    attempts,
    reread,
    the_printer_reports,
)
from backend.tests.unit.services.test_background_dispatch import dispatch_claim_db  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["IDLE", "FINISH"])
async def test_known_context_waits_without_preparing_again(
    db_session,
    test_engine,
    tmp_path,
    printer_factory,
    archive_factory,
    monkeypatch,
    sessions,
    runtime,
    stall_clock,
    state,
):
    import backend.app.services.background_dispatch as bd
    from backend.app.services import dispatch_stall_tracker as tracker

    a = await a_finished_run_held_over_a_pending_job(
        db_session, test_engine, tmp_path, printer_factory, archive_factory, monkeypatch
    )
    the_printer_reports(a.mqtt, state)
    before = await attempts(db_session, a)
    checked = None
    for _ in range(3):
        await one_tick(a.scheduler, a.spawned)
        item = await reread(db_session, PrintQueueItem, a.pending_id)
        if checked is None:
            checked = item.waiting_reason_checked_at
            assert checked is not None
        else:
            assert item.waiting_reason_checked_at == checked, "an unchanged wait was cleared and rewritten"
        stall_clock.advance(40)
    assert await attempts(db_session, a) == before
    item = await reread(db_session, PrintQueueItem, a.pending_id)
    assert item.status == "pending" and item.dispatch_attempts == 0
    assert item.waiting_reason_code == "dispatch_runtime_conflict"
    assert current_print_run(printer_manager, a.printer_id) is a.run
    assert not tracker.snapshot(a.printer_id)  # waiting is not an attempted dispatch
    bd.upload_file_async.assert_not_awaited()
    a.mqtt._client.publish.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["library", "archive"])
async def test_late_conflict_preserves_the_new_run_and_uses_the_context_wait_code(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions, runtime, kind
):
    import backend.app.services.background_dispatch as bd

    queue_id, mqtt, item_id, service, scheduler, spawned = await a_job_and_its_dispatch(
        db_session, test_engine, tmp_path, printer_factory, monkeypatch, kind
    )
    from backend.app.models.printer_queue import PrinterQueue

    printer_id = (await db_session.get(PrinterQueue, queue_id)).printer_id
    holder = None

    async def external_start_during_upload(*_args, **_kwargs):
        nonlocal holder
        holder = bind_print_run(printer_manager, printer_id=printer_id, archive_id=900_001, origin="external")
        return True

    monkeypatch.setattr(bd, "upload_file_async", AsyncMock(side_effect=external_start_during_upload))
    await one_tick(scheduler, spawned)
    item = await reread(db_session, PrintQueueItem, item_id)
    assert item.status == "pending" and item.dispatch_attempts == 0
    assert item.waiting_reason_code == "dispatch_runtime_conflict"
    assert current_print_run(printer_manager, printer_id) is holder
    mqtt._client.publish.assert_not_called()
    bd.upload_file_async.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["print_library_file", "reprint_archive"])
async def test_direct_refusal_precedes_source_capture(monkeypatch, runtime, dispatch_claim_db, kind):
    from backend.app.services import queue_source_capture

    service = BackgroundDispatchService()
    monkeypatch.setattr(service, "_printer_is_busy_printing", lambda *_: False)
    capture = AsyncMock(side_effect=AssertionError("capture must not start"))
    monkeypatch.setattr(queue_source_capture, "capture_staged", capture)
    held = bind_print_run(printer_manager, printer_id=17, archive_id=91)
    with pytest.raises(DispatchEnqueueRejected) as rejected:
        await service._dispatch(
            kind=kind,
            source_id=22 if kind == "print_library_file" else 1,
            source_name="synthetic",
            printer_id=17,
            printer_name="synthetic",
            options={},
            requested_by_user_id=None,
            requested_by_username=None,
        )
    assert rejected.value.detail()["code"] == "dispatch_runtime_conflict"
    assert rejected.value.detail()["printer_id"] == 17 and rejected.value.detail()["message"]
    capture.assert_not_awaited()
    assert not service.has_work_for_printer(17)
    assert current_print_run(printer_manager, 17) is held


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["print_library_file", "reprint_archive"])
async def test_direct_rechecks_after_capture_and_discards_only_its_staging(
    monkeypatch, runtime, dispatch_claim_db, kind
):
    from backend.app.services import queue_batch, queue_source_capture

    service = BackgroundDispatchService()
    monkeypatch.setattr(service, "_printer_is_busy_printing", lambda *_: False)
    staged = SimpleNamespace(receipt=SimpleNamespace(swap_fact=SimpleNamespace(value=False)))

    async def a_run_arrived_while_copying(*_args, **_kwargs):
        bind_print_run(printer_manager, printer_id=17, archive_id=91)
        return staged

    monkeypatch.setattr(queue_source_capture, "capture_staged", AsyncMock(side_effect=a_run_arrived_while_copying))
    discard = AsyncMock()
    monkeypatch.setattr(queue_source_capture, "discard_staged", discard)
    claim = AsyncMock(side_effect=AssertionError("claimed after a known runtime conflict"))
    monkeypatch.setattr(queue_batch, "claim_printer_for_direct_print", claim)
    with pytest.raises(DispatchEnqueueRejected) as rejected:
        await service._dispatch(
            kind=kind,
            source_id=22 if kind == "print_library_file" else 1,
            source_name="synthetic",
            printer_id=17,
            printer_name="synthetic",
            options={},
            requested_by_user_id=None,
            requested_by_username=None,
        )
    assert rejected.value.detail()["code"] == "dispatch_runtime_conflict"
    discard.assert_awaited_once_with(staged)
    claim.assert_not_awaited()
    assert not service.has_work_for_printer(17)
    assert current_print_run(printer_manager, 17).archive_id == 91


@pytest.mark.parametrize(
    "phase", ["_print_run_finishing_bindings", "_print_start_resolutions", "_pending_print_terminals"]
)
def test_a_phase_without_current_does_not_veto(phase):
    manager = SimpleNamespace(**{phase: {1: SimpleNamespace(effect_generation=1, subtask_id=None)}})
    assert not prepared_print_run_conflicts(manager, 1)
    assert bind_prepared_print_run(manager, printer_id=1, archive_id=2) is not None


def test_the_early_veto_is_stricter_than_numeric_archive_id_reuse():
    manager = SimpleNamespace()
    bind_print_run(manager, printer_id=1, archive_id=7)
    assert prepared_print_run_conflicts(manager, 1)
    assert not prepared_print_run_conflicts(manager, 1, archive_id=7)
    assert prepared_print_run_conflicts(manager, 1, archive_id=8)
    assert bind_prepared_print_run(manager, printer_id=1, archive_id=8) is None
    assert bind_prepared_print_run(manager, printer_id=1, archive_id=7) is not None


def test_dispatch_readiness_does_not_change_the_idle_test_used_by_drying(monkeypatch, runtime):
    from backend.app.services.print_scheduler import PrintScheduler

    scheduler = PrintScheduler()
    monkeypatch.setattr(scheduler, "_is_printer_idle", lambda *_args, **_kwargs: True)
    bind_print_run(printer_manager, printer_id=17, archive_id=91)
    assert scheduler._is_printer_idle(17, require_plate_clear=False)
    assert not scheduler._is_printer_ready_for_dispatch(17)
    assert scheduler._is_printer_ready_for_dispatch(18)


@pytest.mark.asyncio
async def test_router_rank_and_rebalance_read_the_same_context_veto(
    db_session, printer_factory, auto_scheduler, routing_item, runtime
):
    from sqlalchemy import select

    from backend.app.services.queue_rebalance import load_printers

    held, _ = await _make_printer_with_queue(db_session, printer_factory, name="held", model="A1MINI")
    ready, ready_queue = await _make_printer_with_queue(db_session, printer_factory, name="ready", model="A1MINI")
    db_session.add(routing_item(target_model="A1MINI", status="pending", position=1))
    await db_session.commit()
    bind_print_run(printer_manager, printer_id=held.id, archive_id=900_001)
    eligibility, scheduler = _patch_printer_manager({held.id, ready.id})
    with eligibility, scheduler:
        printers = await load_printers(db_session, set(), set())
        assert not printers[held.id].ready and printers[ready.id].ready
        await auto_scheduler.tick()
    placed = (await db_session.scalars(select(PrintQueueItem))).one()
    assert placed.queue_id == ready_queue.id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "detail",
    [
        "claim_missing",
        "claim_released",
        "claim_restarted",
        "routing_changed",
        "auto_eject_changed",
        "source_link_changed",
    ],
)
async def test_only_the_current_run_detail_changes_the_wait_class(db_session, printer_factory, detail):
    from backend.app.services.filament_deferred import defer_claim

    _printer, _queue, item, job = await a_claimed_item(db_session, printer_factory)
    item_id = item.id
    result = await defer_claim(
        db_session,
        item_id=item_id,
        started_at=job.claim_started_at,
        reason="dispatch_claim_changed",
        claim_detail=detail,
    )
    await db_session.commit()
    assert result
    row = await reread(db_session, PrintQueueItem, item_id)
    assert row.waiting_reason_code == "filament_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("auto_eject", [True, False], ids=["automatic-predecessor", "manual-predecessor"])
@pytest.mark.parametrize("current_run", [False, True], ids=["no-current", "held-current"])
async def test_scheduler_preserves_the_automatic_predecessors_plate_decision(
    db_session,
    test_engine,
    tmp_path,
    printer_factory,
    archive_factory,
    monkeypatch,
    sessions,
    runtime,
    auto_eject,
    current_run,
):
    from backend.app.models.printer import Printer
    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.services.order_auto_eject import automatic_predecessor

    queue_id, mqtt, item_id, _service, scheduler, spawned = await a_job_and_its_dispatch(
        db_session, test_engine, tmp_path, printer_factory, monkeypatch, "library"
    )
    printer_id = (await db_session.get(PrinterQueue, queue_id)).printer_id
    predecessor = await archive_factory(
        printer_id,
        status="completed",
        extra_data={"dispatch_intent": {"auto_eject": auto_eject, "submission_id": "held-finish"}},
    )
    printer = await db_session.get(Printer, printer_id)
    printer.require_plate_clear = True
    printer.awaiting_plate_clear = True
    printer.awaiting_plate_clear_archive_id = predecessor.id
    await db_session.commit()
    mqtt.state.state = "FINISH"
    mqtt.state.subtask_id = "held-finish"
    monkeypatch.setattr(printer_manager, "is_awaiting_plate_clear", lambda pid: pid == printer_id)
    monkeypatch.setattr(printer_manager, "peek_status", lambda _pid: (mqtt.state, time.monotonic(), False))
    assert not scheduler._is_printer_idle(printer_id)
    assert bool(await automatic_predecessor(db_session, printer)) is auto_eject
    held = bind_print_run(printer_manager, printer_id=printer_id, archive_id=predecessor.id) if current_run else None
    started = AsyncMock(return_value=True)
    monkeypatch.setattr(scheduler, "_start_print", started)
    from backend.app.services import print_scheduler as ps

    recorded_reasons = []
    real_set_wait_reason = ps.set_wait_reason

    def observe_wait(row, code, message):
        if row.id == item_id:
            recorded_reasons.append(code)
        return real_set_wait_reason(row, code, message)

    monkeypatch.setattr(ps, "set_wait_reason", observe_wait)

    await one_tick(scheduler, spawned)

    item = await reread(db_session, PrintQueueItem, item_id)
    if auto_eject and not current_run:
        started.assert_awaited_once()
        assert item.waiting_reason_code is None, "automatic predecessor was falsely labelled a runtime conflict"
        assert "dispatch_runtime_conflict" not in recorded_reasons, (
            "automatic predecessor was falsely labelled in a tick"
        )
    else:
        started.assert_not_awaited()
        assert item.waiting_reason_code == ("dispatch_runtime_conflict" if auto_eject else "plate_not_cleared")
    assert current_print_run(printer_manager, printer_id) is held


@pytest.mark.asyncio
@pytest.mark.parametrize("finishing", [True, False], ids=["finishing-lease", "still-current"])
async def test_scheduler_allows_the_next_job_while_completion_consumers_finish(
    db_session, test_engine, tmp_path, printer_factory, archive_factory, monkeypatch, sessions, runtime, finishing
):
    """A released queue need not wait for the old run's photo/notify/timelapse consumers (R7)."""
    from backend.app.models.printer import Printer
    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.services import print_scheduler as ps

    queue_id, mqtt, item_id, _service, scheduler, spawned = await a_job_and_its_dispatch(
        db_session, test_engine, tmp_path, printer_factory, monkeypatch, "library"
    )
    printer_id = (await db_session.get(PrinterQueue, queue_id)).printer_id
    predecessor = await archive_factory(printer_id, status="completed")
    printer = await db_session.get(Printer, printer_id)
    printer.require_plate_clear = False
    printer.awaiting_plate_clear = False
    await db_session.commit()
    mqtt.state.state = "FINISH"
    monkeypatch.setattr(printer_manager, "is_awaiting_plate_clear", lambda _pid: False)
    monkeypatch.setattr(printer_manager, "peek_status", lambda _pid: (mqtt.state, time.monotonic(), False))
    run = bind_print_run(printer_manager, printer_id=printer_id, archive_id=predecessor.id)
    if finishing:
        assert begin_print_run_finishing(printer_manager, printer_id, predecessor.id) is run
        assert current_print_run(printer_manager, printer_id) is None
        assert finishing_print_runs(printer_manager, printer_id) == (run,)
    assert scheduler._is_printer_idle(printer_id)

    started = AsyncMock(return_value=True)
    monkeypatch.setattr(scheduler, "_start_print", started)
    recorded = []
    real_set_wait_reason = ps.set_wait_reason

    def observe_wait(row, code, message):
        if row.id == item_id:
            recorded.append(code)
        return real_set_wait_reason(row, code, message)

    monkeypatch.setattr(ps, "set_wait_reason", observe_wait)
    await one_tick(scheduler, spawned)

    item = await reread(db_session, PrintQueueItem, item_id)
    if finishing:
        started.assert_awaited_once()
        assert item.waiting_reason_code is None
        assert "dispatch_runtime_conflict" not in recorded, recorded
        assert current_print_run(printer_manager, printer_id) is None
        assert finishing_print_runs(printer_manager, printer_id) == (run,)
    else:
        started.assert_not_awaited()
        assert item.waiting_reason_code == "dispatch_runtime_conflict"
        assert current_print_run(printer_manager, printer_id) is run
