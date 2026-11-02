"""An interrupted dispatch is never closed as a print, and is recovered only by proof (post-rebuild I).

Spec: vault ``60-specs/interrupted-dispatch-info-recovery-spec.md`` — I-R1 (the durable
dispatch phase vetoes a filename verdict), I-R2 (an ended owner is proven by the
installation's own lock and another boot, never by a timer), I-R3 (only a ``prepared``
attempt of an ended owner goes back to the queue by itself, and only when no swap start
ran), I-R4 (Info closes an interrupted attempt, Retry stays separate), acceptance rows
I01–I10. Every printer observation is synthetic; no physical command may be sent.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import backend.app.models.printer_location  # noqa: F401 — resolves Printer's relationship
from backend.app.core.permissions import Permission
from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.print_state_recovery_action import PrintStateRecoveryAction
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import (
    interrupted_dispatch,
    print_state_recovery_preview as tokens,
    print_state_recovery_store as store,
    swap_compatibility,
    swap_terminal,
)
from backend.app.services.farm_startup_guard import FarmStartupGuard, farm_startup_guard
from backend.app.services.print_reconciliation import _reconcile
from backend.app.services.print_scheduler import DISPATCH_MAX_ATTEMPTS
from backend.app.utils import failure_reasons
from backend.tests.unit.services.test_terminal_queue_repair import telemetry  # noqa: F401

pytestmark = pytest.mark.integration

AN_HOUR_AGO = datetime.now(timezone.utc) - timedelta(hours=1)
#: What ``archive_print`` writes on every new execution archive; with no start record beside it,
#: ``swap_terminal.cycle_state`` answers ``not_started``.
NOT_STARTED = {swap_compatibility.CARRIER_KEY: {"v": 1}}


async def an_attempt(db, printer_id: int, *, phase: str | None, boot: str | None = "a-boot-that-ended"):
    archive = PrintArchive(
        printer_id=printer_id,
        filename="plate.gcode.3mf",
        print_name="plate",
        file_path="x/plate.gcode.3mf",
        file_size=1,
        status="printing",
        created_at=AN_HOUR_AGO,
        dispatch_phase=phase,
        dispatch_boot_id=boot,
    )
    db.add(archive)
    await db.commit()
    await db.refresh(archive)
    return archive


@pytest.fixture
def owner(monkeypatch):
    """This process holds the installation's owner lock as ``this-boot``; the farm is released."""
    state = {"boot": "this-boot", "allowed": True}
    monkeypatch.setattr(farm_startup_guard, "boot_id", lambda: state["boot"])
    monkeypatch.setattr(farm_startup_guard, "allowed", lambda: state["allowed"])
    return state


@pytest.fixture
def no_physical_commands(monkeypatch):
    """I10: recovery and Info never reach a printer — every command door is a recording mock."""
    from backend.app.services.printer_manager import printer_manager

    doors = {
        "start_print": Mock(return_value=False),
        "stop_print": Mock(return_value=False),
        "execute_macro_and_wait": AsyncMock(),
        "get_client": Mock(return_value=None),
    }
    for name, door in doors.items():
        monkeypatch.setattr(printer_manager, name, door)
    yield doors
    for name, door in doors.items():
        assert not door.called, f"recovery used the printer command door {name}"


async def an_interrupted_job(
    db,
    printer_factory,
    archive_factory,
    *,
    phase="prepared",
    boot="a-boot-that-ended",
    attempts=1,
    extra=NOT_STARTED,
    queue_status="printing",
):
    """The rows a dispatch leaves when its process ends after inserting the execution archive."""
    printer = await printer_factory()
    source = await archive_factory(printer.id, status="completed", completed_at=AN_HOUR_AGO)
    queue = PrinterQueue(printer_id=printer.id, status=queue_status)
    db.add(queue)
    await db.flush()
    archive = PrintArchive(
        printer_id=printer.id,
        queue_id=queue.id,
        filename="plate.gcode.3mf",
        print_name="plate",
        file_path="x/plate.gcode.3mf",
        file_size=1,
        status="printing",
        created_at=AN_HOUR_AGO,
        dispatch_phase=phase,
        dispatch_boot_id=boot,
        extra_data=deepcopy(extra),
    )
    db.add(archive)
    await db.flush()
    item = PrintQueueItem(
        queue_id=queue.id,
        archive_id=archive.id,
        status="printing",
        started_at=AN_HOUR_AGO,
        dispatch_attempts=attempts,
        plate_id=2,
        ams_mapping="[0, 3]",
        source_snapshot={"version": 1, "provenance": {"kind": "archive", "id": source.id}},
    )
    db.add(item)
    await db.flush()
    if queue_status == "printing":
        queue.current_item_id = item.id
    await db.commit()
    return printer, queue, archive, item, source


async def reread(db, *rows):
    for row in rows:
        await db.refresh(row)


# ── I01 · the durable phase vetoes a filename verdict ────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["prepared", "sending", "sent"])
async def test_an_old_finish_of_the_same_file_never_completes_an_unaccepted_attempt(db_session, printer_factory, phase):
    """I01 / Opus I-1: the printer still shows the PREVIOUS print of this plate as FINISH."""
    printer = await printer_factory()
    archive = await an_attempt(db_session, printer.id, phase=phase)

    with patch("backend.app.services.usage_tracker.on_print_complete", new_callable=AsyncMock) as book:
        closed = await _reconcile(db_session, printer.id, "FINISH", "plate.gcode.3mf")

    assert closed == []
    await db_session.refresh(archive)
    assert archive.status == "printing"
    book.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [None, "accepted"])
async def test_a_print_that_ran_still_closes_as_the_printer_reports(db_session, printer_factory, phase):
    """Control: an accepted attempt, and an archive from before the phase existed (or an
    external print, which never has one), keep the release reconciliation."""
    printer = await printer_factory()
    archive = await an_attempt(db_session, printer.id, phase=phase, boot=None if phase is None else "old")

    with patch("backend.app.services.usage_tracker.on_print_complete", new_callable=AsyncMock):
        closed = await _reconcile(db_session, printer.id, "FINISH", "plate.gcode.3mf")

    assert closed == [archive.id]
    await db_session.refresh(archive)
    assert archive.status == "completed"


# ── I02 / I07 · a prepared attempt of an ended owner goes back to the queue ──


@pytest.mark.asyncio
async def test_a_prepared_attempt_of_an_ended_owner_goes_back_to_the_queue(
    db_session, printer_factory, archive_factory, telemetry, owner, no_physical_commands
):
    """I02: every I-R3 predicate holds. The job is pending again with its own bytes, plate and
    mapping; its spent attempt stays spent (I07); the source archive is untouched."""
    printer, queue, archive, item, source = await an_interrupted_job(db_session, printer_factory, archive_factory)
    snapshot = deepcopy(item.source_snapshot)

    assert await interrupted_dispatch.recover_interrupted_dispatches(printer.id) == [interrupted_dispatch.REQUEUED]

    await reread(db_session, archive, item, queue, source)
    assert archive.status == "failed"
    assert archive.failure_reason == failure_reasons.DISPATCH_PREPARATION_ERROR
    assert archive.completed_at is not None
    assert item.status == "pending"
    assert item.archive_id == source.id and item.library_file_id is None
    assert item.started_at is None and item.error_code is None and item.error_message is None
    assert item.dispatch_attempts == 1
    assert (item.plate_id, item.ams_mapping, item.source_snapshot) == (2, "[0, 3]", snapshot)
    assert queue.status == "idle" and queue.current_item_id is None
    assert source.status == "completed"


@pytest.mark.asyncio
async def test_a_requeued_attempt_carries_no_stale_wait_and_the_change_is_announced(
    db_session, printer_factory, archive_factory, telemetry, owner, monkeypatch
):
    """The requeued job waits for nothing it waited for before the crash, and the open queue
    and archive views learn of the move without a reload."""
    from backend.app.core.websocket import ws_manager

    printer, queue, archive, item, _ = await an_interrupted_job(db_session, printer_factory, archive_factory)
    item.waiting_reason, item.waiting_reason_code = "An old wait", "filament_unavailable"
    item.waiting_reason_checked_at = AN_HOUR_AGO
    await db_session.commit()
    queue_changed, archive_updated = AsyncMock(), AsyncMock()
    monkeypatch.setattr(ws_manager, "send_queue_changed", queue_changed)
    monkeypatch.setattr(ws_manager, "send_archive_updated", archive_updated)

    assert await interrupted_dispatch.recover_interrupted_dispatches(printer.id) == [interrupted_dispatch.REQUEUED]

    await reread(db_session, item)
    assert item.waiting_reason is None and item.waiting_reason_code is None
    assert item.waiting_reason_checked_at is None
    queue_changed.assert_awaited_once_with(printer.id)
    archive_updated.assert_awaited_once_with({"id": archive.id})


@pytest.mark.asyncio
async def test_the_requeue_locks_the_queue_before_the_job_as_every_recovery_writer_does(
    db_session, test_engine, printer_factory, archive_factory, telemetry, owner
):
    """Archive → queue → job, the row order of operator print-state recovery."""
    import re

    from sqlalchemy import event

    printer, queue, archive, item, _ = await an_interrupted_job(db_session, printer_factory, archive_factory)
    statements = []

    def seen(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", seen)
    try:
        assert await interrupted_dispatch.recover_interrupted_dispatches(printer.id) == [interrupted_dispatch.REQUEUED]
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", seen)

    def first(pattern):
        return next(i for i, s in enumerate(statements) if re.search(pattern, s, re.S))

    assert first(r"FROM printer_queues\s+WHERE printer_queues\.id =") < first(
        r"FROM print_queue\s+WHERE print_queue\.id ="
    )


@pytest.mark.asyncio
async def test_an_unexecuted_start_intent_does_not_block_the_requeue(
    db_session, printer_factory, archive_factory, telemetry, owner
):
    """I07 positive control: a pending swap-start INTENT with a carrier that proves no start."""
    printer, queue, archive, item, _ = await an_interrupted_job(
        db_session,
        printer_factory,
        archive_factory,
        extra={**NOT_STARTED, "swap_macro_events_pending": ["swap_mode_start"]},
    )

    assert await interrupted_dispatch.recover_interrupted_dispatches(printer.id) == [interrupted_dispatch.REQUEUED]
    await reread(db_session, item)
    assert item.status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {**NOT_STARTED, swap_terminal.START_EFFECT_KEY: swap_terminal.START_FIRED},
        {**NOT_STARTED, swap_terminal.START_EFFECT_KEY: swap_terminal.START_POSSIBLY_FIRED},
        {},  # no carrier: nothing proves the start did not run
    ],
    ids=["fired", "possibly_fired", "unknown"],
)
async def test_a_swap_start_that_ran_or_may_have_fails_the_job_and_halts_the_queue(
    db_session, printer_factory, archive_factory, telemetry, owner, no_physical_commands, extra
):
    """I07: started / possibly started / unknown veto every automatic pending (shared with D)."""
    printer, queue, archive, item, _ = await an_interrupted_job(
        db_session, printer_factory, archive_factory, extra=extra
    )

    assert await interrupted_dispatch.recover_interrupted_dispatches(printer.id) == [interrupted_dispatch.FAILED]

    await reread(db_session, archive, item, queue)
    assert archive.status == "failed"
    assert item.status == "failed" and item.error_code == failure_reasons.DISPATCH_PREPARATION_ERROR
    assert item.dispatch_attempts == 1
    assert queue.status == "error" and queue.current_item_id is None


@pytest.mark.asyncio
async def test_a_spent_budget_fails_the_job_for_the_operators_retry(
    db_session, printer_factory, archive_factory, telemetry, owner
):
    """I07: the cap is not reset and the crash does not hand the attempt back."""
    printer, queue, archive, item, _ = await an_interrupted_job(
        db_session, printer_factory, archive_factory, attempts=DISPATCH_MAX_ATTEMPTS
    )

    assert await interrupted_dispatch.recover_interrupted_dispatches(printer.id) == [interrupted_dispatch.FAILED]

    await reread(db_session, item, queue)
    assert item.status == "failed" and item.dispatch_attempts == DISPATCH_MAX_ATTEMPTS
    assert queue.status == "error"


@pytest.mark.asyncio
async def test_an_independent_queue_error_is_not_cleared_by_the_requeue(
    db_session, printer_factory, archive_factory, telemetry, owner
):
    """I06: the queue's own error needs its own Resume."""
    printer, queue, archive, item, _ = await an_interrupted_job(
        db_session, printer_factory, archive_factory, queue_status="error"
    )

    assert await interrupted_dispatch.recover_interrupted_dispatches(printer.id) == [interrupted_dispatch.REQUEUED]
    await reread(db_session, item, queue)
    assert item.status == "pending"
    assert queue.status == "error"


# ── I03 / I05 · no false proof of an ended owner ─────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "sending",
        "sent",
        "lock_not_held",
        "same_boot",
        "legacy_boot",
        "printer_running",
        "status_stale",
        "dispatch_in_flight",
        "farm_held",
        "other_effect",
        "other_printing_archive",
    ],
)
async def test_nothing_moves_without_every_proof(
    db_session, printer_factory, archive_factory, telemetry, owner, no_physical_commands, monkeypatch, case
):
    """I03/I05/I06/I07: each missing predicate leaves the attempt exactly as it is — there is no
    timer from ``created_at`` that would end it later; it stays visible in Info."""
    from backend.app.services.background_dispatch import background_dispatch
    from backend.app.services.printer_manager import printer_manager

    kwargs = {}
    if case in ("sending", "sent"):
        kwargs["phase"] = case
    elif case == "same_boot":
        kwargs["boot"] = "this-boot"
    elif case == "legacy_boot":
        kwargs["boot"] = None
    elif case == "other_effect":
        kwargs["extra"] = {**NOT_STARTED, "swap_hold": {"reason": "kept"}}
    printer, queue, archive, item, _ = await an_interrupted_job(db_session, printer_factory, archive_factory, **kwargs)
    if case == "lock_not_held":
        owner["boot"] = None
    elif case == "printer_running":
        telemetry.state = "RUNNING"
    elif case == "status_stale":
        monkeypatch.setattr(printer_manager, "peek_status", Mock(return_value=(telemetry, 10.0, True)))
    elif case == "dispatch_in_flight":
        monkeypatch.setattr(background_dispatch, "has_work_for_printer", Mock(return_value=True))
    elif case == "farm_held":
        owner["allowed"] = False
    elif case == "other_printing_archive":
        await an_attempt(db_session, printer.id, phase=None, boot=None)

    assert await interrupted_dispatch.recover_interrupted_dispatches(printer.id) == []

    await reread(db_session, archive, item, queue)
    assert archive.status == "printing" and archive.dispatch_phase == kwargs.get("phase", "prepared")
    assert item.status == "printing" and item.dispatch_attempts == 1
    assert queue.status == "printing" and queue.current_item_id == item.id


@pytest.mark.asyncio
async def test_a_fault_inside_the_move_leaves_every_row_as_it_was(
    db_session, printer_factory, archive_factory, telemetry, owner, monkeypatch
):
    """I09 (SQLite half): archive, job, claim and counters move in one transaction or not at all."""
    from backend.app.services import queue_counters

    printer, queue, archive, item, source = await an_interrupted_job(db_session, printer_factory, archive_factory)

    async def broken(*_args, **_kwargs):
        raise RuntimeError("counter write failed")

    monkeypatch.setattr(queue_counters, "update_queue_counters", broken)
    with pytest.raises(RuntimeError, match="counter write failed"):
        await interrupted_dispatch.recover_interrupted_dispatches(printer.id)

    await reread(db_session, archive, item, queue)
    assert archive.status == "printing" and archive.failure_reason is None
    assert item.status == "printing" and item.archive_id == archive.id
    assert queue.status == "printing" and queue.current_item_id == item.id


@pytest.mark.asyncio
async def test_the_move_rechecks_the_owner_under_its_locks(
    db_session, printer_factory, archive_factory, telemetry, owner, monkeypatch
):
    """I08: between the selection and the locks this process took the attempt over (a new
    attempt of THIS boot) — the move re-reads and leaves it alone."""
    printer, queue, archive, item, _ = await an_interrupted_job(db_session, printer_factory, archive_factory)
    settle = interrupted_dispatch._settle

    async def a_new_attempt_lands_first(*args):
        archive.dispatch_boot_id = "this-boot"
        await db_session.commit()
        return await settle(*args)

    monkeypatch.setattr(interrupted_dispatch, "_settle", a_new_attempt_lands_first)
    assert await interrupted_dispatch.recover_interrupted_dispatches(printer.id) == []
    await reread(db_session, archive, item)
    assert archive.status == "printing" and item.status == "printing"


@pytest.mark.asyncio
async def test_the_farm_resume_rechecks_the_held_attempt_once(
    db_session, printer_factory, archive_factory, telemetry, owner
):
    """I06: under the farm hold the attempt is visible in Info, not pending; the explicit Resume
    repeats the addressed decision, and a second recheck changes nothing."""
    printer, queue, archive, item, _ = await an_interrupted_job(db_session, printer_factory, archive_factory)
    owner["allowed"] = False
    assert await interrupted_dispatch.recover_interrupted_dispatches(printer.id) == []
    page = await store.list_issues(db_session, None)
    assert any(issue["code"] == "interrupted_dispatch" and issue["item_id"] == item.id for issue in page["issues"])

    owner["allowed"] = True
    await interrupted_dispatch.recover_after_resume()
    await reread(db_session, item, archive)
    assert item.status == "pending" and archive.status == "failed"
    completed_at = archive.completed_at

    await interrupted_dispatch.recover_after_resume()
    await reread(db_session, item, archive)
    assert item.status == "pending" and archive.completed_at == completed_at
    assert item.dispatch_attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("first_look", ["printer_busy", "status_stale"])
async def test_a_requeue_that_could_not_run_yet_runs_on_the_printers_next_idle_status(
    db_session, printer_factory, archive_factory, telemetry, owner, monkeypatch, first_look
):
    """M1: the connection's first status (or the Resume) found the printer busy or stale; a
    later status of the SAME connection that shows it fresh and idle rechecks once — no new
    Resume, no reconnect, no timer, and a status that changes nothing runs nothing."""
    from backend.app.services.printer_manager import printer_manager

    printer, queue, archive, item, _ = await an_interrupted_job(db_session, printer_factory, archive_factory)
    if first_look == "printer_busy":
        telemetry.state = "RUNNING"
    else:
        monkeypatch.setattr(printer_manager, "peek_status", Mock(return_value=(telemetry, 10.0, True)))
    assert await interrupted_dispatch.recover_interrupted_dispatches(printer.id) == []
    assert not interrupted_dispatch.recheck_due(printer.id)  # nothing changed yet

    telemetry.state = "IDLE"
    monkeypatch.setattr(printer_manager, "peek_status", Mock(return_value=(telemetry, 11.0, False)))
    assert interrupted_dispatch.recheck_due(printer.id)
    assert not interrupted_dispatch.recheck_due(printer.id)  # once
    assert await interrupted_dispatch.recover_interrupted_dispatches(printer.id) == [interrupted_dispatch.REQUEUED]
    await reread(db_session, item)
    assert item.status == "pending"


@pytest.mark.asyncio
async def test_a_printer_with_nothing_to_requeue_waits_for_nothing(
    db_session, printer_factory, archive_factory, telemetry, owner
):
    """An unaccepted attempt that is not ``prepared`` is the operator's: no recheck is armed."""
    printer, queue, archive, item, _ = await an_interrupted_job(
        db_session, printer_factory, archive_factory, phase="sent"
    )
    telemetry.state = "RUNNING"
    assert await interrupted_dispatch.recover_interrupted_dispatches(printer.id) == []
    telemetry.state = "IDLE"
    assert not interrupted_dispatch.recheck_due(printer.id)


@pytest.mark.asyncio
async def test_a_due_recheck_runs_once_in_the_background(monkeypatch):
    """The status hook runs the waiting recheck of an idle printer only when it is due, and
    a printer nobody waits for costs nothing (no snapshot is read)."""
    from types import SimpleNamespace

    ran = AsyncMock(return_value=[])
    due = Mock(side_effect=[True, False])
    monkeypatch.setattr(interrupted_dispatch, "recover_interrupted_dispatches", ran)
    monkeypatch.setattr(interrupted_dispatch, "recheck_due", due)
    idle = SimpleNamespace(state="IDLE")

    interrupted_dispatch.on_status(8, idle)  # nobody waits for printer 8
    interrupted_dispatch._waiting.add(7)
    interrupted_dispatch.on_status(7, SimpleNamespace(state="RUNNING"))  # not idle: not asked
    interrupted_dispatch.on_status(7, idle)
    interrupted_dispatch.on_status(7, idle)
    for _ in range(3):
        await asyncio.sleep(0)

    assert due.call_count == 2
    ran.assert_awaited_once_with(7)


@pytest.mark.asyncio
async def test_the_printer_status_callback_hands_every_status_to_the_recheck(monkeypatch):
    """Wired before the broadcast's dedup: a status that changes nothing on the card (stale →
    fresh on the same state) must still reach the recheck."""
    from backend.app import main
    from backend.tests.unit.test_printer_offline_notification import TestOfflineEdgeDetection, _state

    hook = Mock()
    monkeypatch.setattr(interrupted_dispatch, "on_status", hook)
    state = _state(connected=True)
    ws_mgr, relay, pm = TestOfflineEdgeDetection._patch_handler_deps()
    with (
        patch("backend.app.main.ws_manager", ws_mgr),
        patch("backend.app.main.mqtt_relay", relay),
        patch("backend.app.main.printer_manager", pm),
        patch("backend.app.main.printer_state_to_dict", return_value={}),
    ):
        await main.on_printer_status_change(1, state)
        await main.on_printer_status_change(1, state)

    assert hook.call_count == 2
    hook.assert_called_with(1, state)


def test_the_guard_proves_nothing_once_it_stops_holding_the_lock(tmp_path, monkeypatch):
    """I03: a boot is proof only while the lock is held, and never over a storage error."""
    guard = FarmStartupGuard()
    assert guard.boot_id() is None
    guard.start(tmp_path)
    try:
        assert guard.boot_id() == guard.state["boot_id"]
        guard.storage_error = True
        assert guard.boot_id() is None
        guard.storage_error = False
    finally:
        guard.finish(clean=True)
    assert guard.boot_id() is None


@pytest.mark.asyncio
async def test_the_lock_is_released_only_after_every_command_io_has_drained(monkeypatch):
    """I-R2: ``finish`` unlocks after the dispatch writers and their I/O ended — a later process
    can never read this boot as ended while one of them may still send."""
    from backend.app import main
    from backend.app.services import farm_startup_guard as guard_module

    calls: list[str] = []
    drained = asyncio.Event()
    drain_started = asyncio.Event()

    @asynccontextmanager
    async def application(app):
        yield
        calls.append("application_stopped")

    async def drain():
        calls.append("drain_started")
        drain_started.set()
        await drained.wait()
        calls.append("drain_finished")

    monkeypatch.setattr(main, "_application_lifespan", application)
    monkeypatch.setattr(guard_module, "drain_commands", drain)
    monkeypatch.setattr(main.farm_startup_guard, "start", lambda _path: calls.append("locked"))
    monkeypatch.setattr(main.farm_startup_guard, "close_admission", lambda: None)
    monkeypatch.setattr(main.farm_startup_guard, "finish", lambda *, clean: calls.append("unlocked"))

    async def run():
        async with main.lifespan(main.app):
            pass

    task = asyncio.create_task(run())
    await asyncio.wait_for(drain_started.wait(), timeout=10)
    assert calls == ["locked", "application_stopped", "drain_started"]
    drained.set()
    await task
    assert calls[-2:] == ["drain_finished", "unlocked"]


# ── I08 · Info closes an interrupted attempt; Retry stays separate ───────────


def factory(test_engine):
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


async def close_through_info(db, test_engine, printer, item):
    preview = await store.make_preview(db, printer.id, "item", item.id, "close_interrupted_attempt", "user:1")
    payload = tokens.verify(preview["preview_token"])
    return preview, payload, await store.apply_preview(factory(test_engine), payload, "user:1", "Operator")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase, code",
    [
        ("prepared", failure_reasons.DISPATCH_PREPARATION_ERROR),
        ("sending", failure_reasons.DISPATCH_NO_ACKNOWLEDGEMENT),
        ("sent", failure_reasons.DISPATCH_NO_ACKNOWLEDGEMENT),
    ],
)
async def test_info_closes_an_interrupted_attempt_and_leaves_retry_to_the_operator(
    db_session, test_engine, printer_factory, archive_factory, telemetry, owner, no_physical_commands, phase, code
):
    """I08: archive and job failed with the dispatch code, the queue stops with an error; the job
    is not pending (Retry is a separate action), the attempt is not handed back, and a repeated
    apply answers the same result without a second change."""
    owner["allowed"] = False  # a held farm does not stop the operator from closing an attempt
    printer, queue, archive, item, source = await an_interrupted_job(
        db_session, printer_factory, archive_factory, phase=phase
    )
    page = await store.list_issues(db_session, None)
    issue = next(row for row in page["issues"] if row["kind"] == "item" and row["target_id"] == item.id)
    assert issue["code"] == "interrupted_dispatch"
    assert issue["actions"] == ["close_interrupted_attempt"]
    assert issue["blocked_reasons"] == []

    preview, payload, result = await close_through_info(db_session, test_engine, printer, item)
    assert preview["after"]["archive"] == {"status": "failed", "failure_reason": code}
    repeated = await store.apply_preview(factory(test_engine), payload, "user:1", "Operator")
    assert repeated == result

    await reread(db_session, archive, item, queue, source)
    assert archive.status == "failed" and archive.failure_reason == code
    assert archive.error_message and archive.completed_at is not None
    assert archive.dispatch_phase == phase  # history: how far the attempt got
    assert item.status == "failed" and item.error_code == code and item.error_message == archive.error_message
    assert item.completed_at is not None and item.dispatch_attempts == 1
    assert item.archive_id == archive.id  # Retry restores the source; the close does not
    assert queue.status == "error" and queue.current_item_id is None
    assert source.status == "completed"
    assert await db_session.scalar(select(func.count()).select_from(PrintStateRecoveryAction)) == 1


def watched_no_more(printer_id, archive_id):
    """What a runner of THIS process leaves when it stops watching a sent start without the
    printer's answer: its prepared run still bound, and the attempt marked unobserved."""
    from backend.app.services import background_dispatch as bd
    from backend.app.services.print_run_binding import bind_prepared_print_run
    from backend.app.services.printer_manager import printer_manager

    assert bind_prepared_print_run(printer_manager, printer_id=printer_id, archive_id=archive_id) is not None
    bd.mark_unobserved(archive_id)


@pytest.mark.asyncio
async def test_info_closes_an_attempt_this_process_stopped_watching(
    db_session, test_engine, printer_factory, archive_factory, telemetry, owner, no_physical_commands
):
    """D-R4 / I08: the owner is alive — this process — and says itself that no runner watches
    the sent start any more; on a fresh idle printer whose only run is this attempt's own,
    the operator may close it without a restart. The owner is never called ended."""
    printer, queue, archive, item, _ = await an_interrupted_job(
        db_session, printer_factory, archive_factory, phase="sent", boot="this-boot"
    )
    watched_no_more(printer.id, archive.id)

    page = await store.list_issues(db_session, None)
    issue = next(row for row in page["issues"] if row["kind"] == "item" and row["target_id"] == item.id)
    assert issue["code"] == "interrupted_dispatch"
    assert issue["actions"] == ["close_interrupted_attempt"] and issue["blocked_reasons"] == []

    await close_through_info(db_session, test_engine, printer, item)
    await reread(db_session, archive, item, queue)
    assert archive.status == "failed" and archive.failure_reason == failure_reasons.DISPATCH_NO_ACKNOWLEDGEMENT
    assert item.status == "failed" and queue.status == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["printer_running", "another_run", "still_watched"])
async def test_info_offers_no_close_of_an_unwatched_attempt_without_an_idle_printer(
    db_session, printer_factory, archive_factory, telemetry, owner, case
):
    """The unwatched attempt still needs a fresh idle printer whose only run is its own, and an
    attempt no runner has given up keeps its owner's reason."""
    from backend.app.services import background_dispatch as bd
    from backend.app.services.print_run_binding import bind_print_run
    from backend.app.services.printer_manager import printer_manager

    printer, queue, archive, item, _ = await an_interrupted_job(
        db_session, printer_factory, archive_factory, phase="sent", boot="this-boot"
    )
    if case == "still_watched":
        bind_print_run(printer_manager, printer_id=printer.id, archive_id=archive.id, origin="dispatch")
    elif case == "another_run":
        bind_print_run(printer_manager, printer_id=printer.id, archive_id=archive.id + 1000, origin="external")
        bd.mark_unobserved(archive.id)
    else:
        watched_no_more(printer.id, archive.id)
        telemetry.state = "RUNNING"

    page = await store.list_issues(db_session, None)
    issue = next(row for row in page["issues"] if row["kind"] == "item" and row["target_id"] == item.id)
    assert issue["actions"] == []
    assert ("owner_alive" if case == "still_watched" else "live_unknown") in issue["blocked_reasons"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case, reason",
    [
        ("same_boot", "owner_alive"),
        ("lock_not_held", "owner_unknown"),
        ("legacy_boot", "owner_unknown"),
        ("printer_running", "live_unknown"),
        ("other_effect", "effects_pending"),
        ("other_printing_archive", "ambiguous_owner"),
    ],
)
async def test_info_offers_no_close_without_proof(
    db_session, printer_factory, archive_factory, telemetry, owner, case, reason
):
    """I08: an owner that may be alive, an unknown one, an active printer, an unfinished effect or
    another running archive is shown with its reason and no action."""
    kwargs = {"phase": "sent"}
    if case == "same_boot":
        kwargs["boot"] = "this-boot"
    elif case == "legacy_boot":
        kwargs["boot"] = None
    elif case == "other_effect":
        kwargs["extra"] = {**NOT_STARTED, "swap_hold": {"reason": "kept"}}
    printer, queue, archive, item, _ = await an_interrupted_job(db_session, printer_factory, archive_factory, **kwargs)
    if case == "lock_not_held":
        owner["boot"] = None
    elif case == "printer_running":
        telemetry.state = "RUNNING"
    elif case == "other_printing_archive":
        await an_attempt(db_session, printer.id, phase=None, boot=None)

    page = await store.list_issues(db_session, None)
    issue = next(row for row in page["issues"] if row["kind"] == "item" and row["target_id"] == item.id)
    assert issue["code"] == "interrupted_dispatch"
    assert issue["actions"] == []
    assert reason in issue["blocked_reasons"]
    with pytest.raises(ValueError, match="recovery_not_allowed"):
        await store.make_preview(db_session, printer.id, "item", item.id, "close_interrupted_attempt", "user:1")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["accepted", "new_attempt", "closed_elsewhere"])
async def test_a_stale_preview_is_refused_without_a_write(
    db_session, test_engine, printer_factory, archive_factory, telemetry, owner, change
):
    """I08: the attempt moved on after the preview — it was accepted, a new attempt B owns the job,
    or another writer already closed it. Apply re-reads under the locks and changes nothing."""
    printer, queue, archive, item, _ = await an_interrupted_job(
        db_session, printer_factory, archive_factory, phase="sent"
    )
    preview = await store.make_preview(db_session, printer.id, "item", item.id, "close_interrupted_attempt", "user:1")
    if change == "accepted":
        archive.dispatch_phase = "accepted"
    elif change == "new_attempt":
        b = await an_attempt(db_session, printer.id, phase="prepared", boot="this-boot")
        archive.status = "aborted"
        item.archive_id = b.id
    else:
        archive.status = "failed"
    await db_session.commit()

    with pytest.raises(ValueError, match="recovery_changed"):
        await store.apply_preview(factory(test_engine), tokens.verify(preview["preview_token"]), "user:1", "Operator")
    await reread(db_session, item, queue)
    assert item.status == "printing" and queue.status == "printing"
    assert await db_session.scalar(select(func.count()).select_from(PrintStateRecoveryAction)) == 0


@pytest.mark.asyncio
async def test_two_concurrent_applies_make_one_transition(
    db_session, test_engine, printer_factory, archive_factory, telemetry, owner
):
    printer, queue, archive, item, _ = await an_interrupted_job(
        db_session, printer_factory, archive_factory, phase="sending"
    )
    preview = await store.make_preview(db_session, printer.id, "item", item.id, "close_interrupted_attempt", "user:1")
    payload = tokens.verify(preview["preview_token"])

    first, second = await asyncio.gather(
        store.apply_preview(factory(test_engine), payload, "user:1", "Operator"),
        store.apply_preview(factory(test_engine), payload, "user:1", "Operator"),
    )
    assert first == second
    assert await db_session.scalar(select(func.count()).select_from(PrintStateRecoveryAction)) == 1


@pytest.mark.asyncio
async def test_the_automatic_requeue_and_the_info_close_never_both_apply(
    db_session, test_engine, printer_factory, archive_factory, telemetry, owner
):
    """I08: a preview taken before the automatic requeue is stale after it."""
    printer, queue, archive, item, _ = await an_interrupted_job(db_session, printer_factory, archive_factory)
    preview = await store.make_preview(db_session, printer.id, "item", item.id, "close_interrupted_attempt", "user:1")
    assert await interrupted_dispatch.recover_interrupted_dispatches(printer.id) == [interrupted_dispatch.REQUEUED]

    with pytest.raises(ValueError, match="recovery_changed"):
        await store.apply_preview(factory(test_engine), tokens.verify(preview["preview_token"]), "user:1", "Operator")
    await reread(db_session, item)
    assert item.status == "pending"


def test_closing_an_attempt_asks_the_queue_and_printer_rights():
    """I08: the same rights as the other queue repairs; an API key reaches it only through them."""
    from backend.app.api.routes.print_state_recovery import WRITE_PERMISSIONS

    assert WRITE_PERMISSIONS["close_interrupted_attempt"] == (Permission.QUEUE_UPDATE_ALL, Permission.PRINTERS_CONTROL)
