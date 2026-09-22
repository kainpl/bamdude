"""Recovery of terminal archive + printing item is structural, never a replay."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.print_reconciliation import (
    _inactive_repair_snapshot,
    _reconcile,
    _repair_terminal_queue_items,
    _RepairSnapshotChanged,
    release_interrupted_dispatch_claims,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture
def telemetry(monkeypatch):
    from backend.app.services.background_dispatch import background_dispatch
    from backend.app.services.print_scheduler import scheduler
    from backend.app.services.printer_manager import printer_manager

    state = PrinterState(connected=True, state="IDLE", connection_generation=1)
    monkeypatch.setattr(printer_manager, "peek_status", Mock(return_value=(state, 10.0, False)))
    monkeypatch.setattr(scheduler, "has_dispatch_in_flight", Mock(return_value=False))
    monkeypatch.setattr(background_dispatch, "has_work_for_printer", Mock(return_value=False))
    return state


async def make_pair(db, printer_factory, archive_factory, *, outcome="completed", header="printing"):
    printer = await printer_factory()
    queue = PrinterQueue(printer_id=printer.id, status=header)
    db.add(queue)
    await db.flush()
    started = datetime.now(timezone.utc) - timedelta(hours=2)
    archive = await archive_factory(
        printer.id,
        queue_id=queue.id,
        status=outcome,
        started_at=started,
        completed_at=started + timedelta(hours=1),
        extra_data={"swap_macro_events_pending": ["swap_mode_change_table"], "historical": True},
    )
    item = PrintQueueItem(queue_id=queue.id, archive_id=archive.id, status="printing", started_at=started)
    pending = PrintQueueItem(queue_id=queue.id, status="pending", position=1)
    db.add_all([item, pending])
    await db.flush()
    if header == "printing":
        queue.current_item_id = item.id
    await db.commit()
    return printer, queue, archive, item, pending


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled", "aborted", "stopped"])
@pytest.mark.parametrize("header", ["printing", "idle", "paused", "error"])
async def test_repairs_only_queue_and_is_idempotent(
    db_session, printer_factory, archive_factory, telemetry, outcome, header
):
    printer, queue, archive, item, pending = await make_pair(
        db_session,
        printer_factory,
        archive_factory,
        outcome=outcome,
        header=header,
    )
    original_extra = dict(archive.extra_data)
    original_completed = archive.completed_at
    with patch(
        "backend.app.services.print_reconciliation._reconcile_complete_archive", new_callable=AsyncMock
    ) as finish:
        repaired = await _repair_terminal_queue_items(db_session, printer.id, "IDLE")
        await db_session.commit()
        assert len(repaired) == 1
        finish.assert_not_awaited()
    await db_session.refresh(item)
    await db_session.refresh(queue)
    await db_session.refresh(archive)
    await db_session.refresh(pending)
    assert item.status == ({"aborted": "cancelled", "stopped": "cancelled"}.get(outcome, outcome))
    assert item.completed_at == original_completed
    assert queue.status == ("error" if header == "error" else "paused")
    assert queue.is_paused and queue.current_item_id is None
    assert queue.pending_count == 1
    assert pending.status == "pending"
    assert archive.status == outcome and archive.extra_data == original_extra
    assert archive.completed_at == original_completed
    assert await _repair_terminal_queue_items(db_session, printer.id, "IDLE") == []


@pytest.mark.parametrize(
    "reason",
    [
        "RUNNING",
        "PAUSE",
        "PREPARE",
        "SLICING",
        "unknown",
        "offline",
        "stale",
        "no_status",
        "dispatch",
        "upload",
        "uncertain",
        "source_archive",
        "source_provenance",
        "missing_time",
        "future_end",
        "wrong_printer",
        "wrong_queue",
        "deleted_archive",
        "missing_archive",
        "shared_archive",
        "other_item",
        "other_archive",
        "different_header",
        "changed_event",
    ],
)
async def test_ambiguous_or_busy_state_is_untouched(db_session, printer_factory, archive_factory, telemetry, reason):
    from backend.app.services.background_dispatch import background_dispatch
    from backend.app.services.print_scheduler import scheduler
    from backend.app.services.printer_manager import printer_manager

    printer, queue, archive, item, pending = await make_pair(db_session, printer_factory, archive_factory)
    if reason in {"RUNNING", "PAUSE", "PREPARE", "SLICING", "unknown"}:
        telemetry.state = reason
    elif reason == "offline":
        telemetry.connected = False
    elif reason == "stale":
        printer_manager.peek_status.return_value = (telemetry, 10.0, True)
    elif reason == "no_status":
        printer_manager.peek_status.return_value = (telemetry, None, False)
    elif reason == "dispatch":
        scheduler.has_dispatch_in_flight.return_value = True
    elif reason == "upload":
        background_dispatch.has_work_for_printer.return_value = True
    elif reason == "uncertain":
        archive.extra_data = {"recovered_outcome_uncertain": True}
    elif reason == "source_archive":
        item.started_at = datetime.now(timezone.utc)
    elif reason == "source_provenance":
        item.source_snapshot = {"provenance": {"kind": "archive", "id": archive.id}}
    elif reason == "missing_time":
        item.started_at = None
    elif reason == "future_end":
        archive.completed_at = datetime.now(timezone.utc) + timedelta(hours=1)
    elif reason == "wrong_printer":
        other = await printer_factory()
        archive.printer_id = other.id
    elif reason == "wrong_queue":
        archive.queue_id = None
    elif reason == "deleted_archive":
        archive.deleted_at = datetime.now(timezone.utc)
    elif reason == "missing_archive":
        item.archive_id = None
    elif reason == "shared_archive":
        db_session.add(
            PrintQueueItem(queue_id=queue.id, status="printing", archive_id=archive.id, started_at=item.started_at)
        )
    elif reason == "other_item":
        pending.status = "printing"
    elif reason == "other_archive":
        await archive_factory(printer.id, status="printing")
    elif reason == "different_header":
        queue.current_item_id = pending.id
    elif reason == "changed_event":
        telemetry.state = "FINISH"
    await db_session.commit()
    assert await _repair_terminal_queue_items(db_session, printer.id, "IDLE") == []
    await db_session.refresh(item)
    await db_session.refresh(queue)
    assert item.status == "printing"
    assert queue.status == "printing" and not queue.is_paused


@pytest.mark.parametrize("change", ["generation", "new_run", "disconnect", "dispatch"])
async def test_snapshot_change_after_flush_rolls_back(
    db_session, printer_factory, archive_factory, telemetry, monkeypatch, change
):
    from backend.app.services.background_dispatch import background_dispatch

    printer, queue, archive, item, pending = await make_pair(db_session, printer_factory, archive_factory)
    item_id, queue_id = item.id, queue.id
    original_flush = db_session.flush

    async def move_on(*args, **kwargs):
        await original_flush(*args, **kwargs)
        if change == "generation":
            telemetry.connection_generation += 1
        elif change == "new_run":
            telemetry.subtask_id = "next-run"
        elif change == "disconnect":
            telemetry.connected = False
        else:
            background_dispatch.has_work_for_printer.return_value = True

    monkeypatch.setattr(db_session, "flush", move_on)
    with pytest.raises(_RepairSnapshotChanged):
        await _repair_terminal_queue_items(db_session, printer.id, "IDLE")
    await db_session.rollback()
    item = await db_session.get(PrintQueueItem, item_id)
    queue = await db_session.get(PrinterQueue, queue_id)
    assert item.status == "printing"
    assert queue.status == "printing" and not queue.is_paused


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled", "printing"])
async def test_startup_does_not_requeue_a_linked_archive(db_session, printer_factory, archive_factory, outcome):
    printer, queue, archive, item, pending = await make_pair(
        db_session, printer_factory, archive_factory, outcome=outcome
    )
    assert await release_interrupted_dispatch_claims(db_session) == 0
    await db_session.refresh(item)
    assert item.status == "printing"


async def test_two_proven_historical_rows_are_repaired_without_deletion(
    db_session, printer_factory, archive_factory, telemetry
):
    printer, queue, archive, item, pending = await make_pair(db_session, printer_factory, archive_factory)
    other = await archive_factory(printer.id, queue_id=queue.id, status="failed", completed_at=archive.completed_at)
    pending.status = "printing"
    pending.archive_id = other.id
    pending.started_at = item.started_at
    await db_session.commit()
    assert len(await _repair_terminal_queue_items(db_session, printer.id, "IDLE")) == 2
    await db_session.commit()
    await db_session.refresh(pending)
    assert pending.status == "failed"


async def test_live_completion_veto_cleans_up_even_on_exception(telemetry):
    from backend.app.main import on_print_complete

    async def fail(*args):
        assert _inactive_repair_snapshot(123) is None
        raise RuntimeError("completion failed")

    with (
        patch("backend.app.main._on_print_complete_impl", side_effect=fail),
        pytest.raises(RuntimeError, match="completion failed"),
    ):
        await on_print_complete(123, {})
    assert _inactive_repair_snapshot(123) is not None


@pytest.mark.parametrize(
    "names",
    [
        ["partX.3mf", "partX.gcode.3mf"],
        ["part.gcodeX.3mf", "part.3mfX.3mf"],
    ],
)
async def test_reconnect_does_not_close_ambiguous_archives(db_session, printer_factory, archive_factory, names):
    printer = await printer_factory()
    for name in names:
        await archive_factory(printer.id, status="printing", filename=name, print_name=None)
    with (
        patch("backend.app.services.print_reconciliation._grace_seconds", AsyncMock(return_value=0)),
        patch(
            "backend.app.services.print_reconciliation._reconcile_complete_archive", new_callable=AsyncMock
        ) as finish,
    ):
        assert await _reconcile(db_session, printer.id, "FINISH", "", "", "partX") == []
        finish.assert_not_awaited()


async def test_first_status_repairs_without_replaying_effects_or_replacing_gate(
    db_session,
    printer_factory,
    archive_factory,
    telemetry,
    monkeypatch,
):
    from backend.app.core import database
    from backend.app.services.print_reconciliation import reconcile_printer_prints

    printer, queue, archive, item, pending = await make_pair(db_session, printer_factory, archive_factory)
    printer.awaiting_plate_clear = True
    printer.awaiting_plate_clear_archive_id = archive.id
    printer.awaiting_plate_clear_token = "unchanged-gate"
    await db_session.commit()

    @asynccontextmanager
    async def session():
        try:
            yield db_session
        except BaseException:
            await db_session.rollback()
            raise

    monkeypatch.setattr(database, "async_session", session)
    with (
        patch(
            "backend.app.services.print_reconciliation._reconcile_complete_archive", new_callable=AsyncMock
        ) as finish,
        patch("backend.app.services.print_reconciliation._resolve_pending_swaps", new_callable=AsyncMock) as swaps,
        patch("backend.app.core.tasks.spawn_background_task") as spawn,
    ):
        await reconcile_printer_prints(printer.id, "IDLE", "")
        await reconcile_printer_prints(printer.id, "IDLE", "")
        finish.assert_not_awaited()
        swaps.assert_not_awaited()
        spawn.assert_not_called()
    await db_session.refresh(printer)
    await db_session.refresh(item)
    assert item.status == "completed"
    assert printer.awaiting_plate_clear
    assert printer.awaiting_plate_clear_archive_id == archive.id
    assert printer.awaiting_plate_clear_token == "unchanged-gate"


async def test_dispatch_veto_includes_queued_and_uploading_jobs():
    from backend.app.services.background_dispatch import BackgroundDispatchService

    dispatch = BackgroundDispatchService()
    assert not dispatch.has_work_for_printer(1)
    dispatch._queued_jobs.append(SimpleNamespace(printer_id=1))
    assert dispatch.has_work_for_printer(1)
    assert not dispatch.has_work_for_printer(2)
    dispatch._queued_jobs.clear()
    dispatch._active_jobs[7] = SimpleNamespace(job=SimpleNamespace(printer_id=1))
    assert dispatch.has_work_for_printer(1)
    assert not dispatch.has_work_for_printer(2)
