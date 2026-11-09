"""Findings of the final whole-branch review of the post-rebuild packages (D, I, F).

Each test names its finding; each failed before its fix.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from itertools import count
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

import backend.app.models.printer_location  # noqa: F401 — resolves Printer's relationship
from backend.app.models.archive import PrintArchive
from backend.app.models.print_usage_event import (
    EVENT_FEED_RETURN,
    EVENT_RUNOUT,
    EVENT_SPOOL_LOADED,
    EVENT_START,
    EVENT_TAIL_START,
    KIND_PAUSE,
)
from backend.app.services import print_usage_journal as journal
from backend.app.services.usage_tracker import journal_boundaries_for_tray
from backend.app.utils import failure_reasons

pytestmark = pytest.mark.integration  # asyncio_mode = "auto" runs the coroutines

OLD, NEW = 309, 313
TRAY = 2
LONG_AGO = datetime.now(timezone.utc) - timedelta(hours=5)


# ── 1 · the print-start cleanup never completes an unaccepted attempt by its timer ──


@pytest.mark.parametrize("phase", ["prepared", "sending", "sent", None])
async def test_the_stale_row_cleanup_leaves_an_unaccepted_attempt_to_its_recovery(db_session, printer_factory, phase):
    from backend.app.services.print_execution import ExecutionChanged, decide_start

    printer = await printer_factory()
    archive = PrintArchive(
        printer_id=printer.id,
        filename="plate.gcode.3mf",
        print_name="plate",
        file_path="x/plate.gcode.3mf",
        file_size=1,
        status="printing",
        started_at=LONG_AGO,
        print_time_seconds=3600,
        dispatch_phase=phase,
        dispatch_boot_id="a-boot-that-ended" if phase else None,
    )
    db_session.add(archive)
    await db_session.commit()
    archive_id = archive.id

    with pytest.raises(ExecutionChanged):
        await decide_start(db_session, printer.id, {"filename": "another-file"})

    db_session.expire_all()
    status = await db_session.scalar(select(PrintArchive.status).where(PrintArchive.id == archive_id))
    assert status == "printing"


# ── 4 · a print seen running marks its attempt accepted; a checkpoint outranks the veto ──


@pytest.fixture
def clear_print_registries():
    from backend.app import main as main_module
    from backend.app.services.printer_manager import printer_manager

    def clear():
        for registry in (
            main_module._active_prints,
            main_module._expected_prints,
            main_module._expected_print_registered_at,
            main_module._expected_print_creators,
        ):
            registry.clear()
        for attribute in (
            "_print_start_admissions",
            "_print_effect_generations",
            "_print_run_bindings",
            "_print_run_finishing_bindings",
            "_print_start_resolutions",
            "_pending_print_terminals",
        ):
            registry = getattr(printer_manager, attribute, None)
            if isinstance(registry, dict):
                registry.clear()

    clear()
    yield
    clear()


async def print_starts(test_engine, monkeypatch, printer_id: int) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    monkeypatch.setattr(
        "backend.app.main.async_session", async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    )
    with (
        patch("backend.app.main.notify_missing_spool_assignments_on_print_start", new_callable=AsyncMock),
        patch("backend.app.main._send_print_start_notification", new_callable=AsyncMock),
        patch("backend.app.main._load_objects_from_archive"),
        patch("backend.app.main._record_energy_start", new_callable=AsyncMock),
        patch("backend.app.services.macro_trigger.fire_event_macros", new_callable=AsyncMock),
        patch("backend.app.main._capture_timelapse_baseline_at_start", new_callable=AsyncMock),
    ):
        from backend.app.main import on_print_start

        await on_print_start(
            printer_id,
            {"filename": "widget.gcode.3mf", "subtask_name": "widget", "subtask_id": "77", "raw_data": {}},
        )


@pytest.mark.parametrize("how", ["expected", "adopted"])
async def test_a_print_seen_starting_marks_its_attempt_accepted(
    db_session, test_engine, monkeypatch, printer_factory, clear_print_registries, how
):
    """Review 4: the slow-parse start (the watchdog had failed it) and the restart adoption
    both prove the printer is running the attempt — it must not stay ``sent`` and vetoed."""
    from backend.app import main as main_module

    printer = await printer_factory()
    archive = PrintArchive(
        printer_id=printer.id,
        filename="widget.gcode.3mf",
        file_path="",
        file_size=0,
        print_name="widget",
        status="failed" if how == "expected" else "printing",
        subtask_id="77",
        extra_data={"dispatch_intent": {"version": 1, "submission_id": "77"}},
        dispatch_phase="sent",
        dispatch_boot_id="this-boot" if how == "expected" else "a-boot-that-ended",
    )
    db_session.add(archive)
    await db_session.commit()
    archive_id = archive.id
    if how == "expected":
        main_module._expected_prints[(printer.id, "widget")] = archive_id
        from backend.app.services.print_run_binding import bind_prepared_print_run

        bind_prepared_print_run(
            main_module.printer_manager,
            printer_id=printer.id,
            archive_id=archive_id,
            archive_identity=archive.recovery_identity,
            expected_submission_id="77",
        )

    await print_starts(test_engine, monkeypatch, printer.id)

    db_session.expire_all()
    status, phase = (
        await db_session.execute(
            select(PrintArchive.status, PrintArchive.dispatch_phase).where(PrintArchive.id == archive_id)
        )
    ).one()
    assert (status, phase) == ("printing", "accepted")


async def test_an_accepted_terminal_checkpoint_outranks_the_unaccepted_veto(db_session, printer_factory):
    """Review 4: on_print_complete accepted a finish for this attempt — it ran."""
    from backend.app.main import _TERMINAL_ACCEPTANCE_KEY
    from backend.app.services import print_reconciliation

    printer = await printer_factory()
    archive = PrintArchive(
        printer_id=printer.id,
        filename="plate.gcode.3mf",
        print_name="plate",
        file_path="x/plate.gcode.3mf",
        file_size=1,
        status="printing",
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        dispatch_phase="sent",
        dispatch_boot_id="a-boot-that-ended",
        extra_data={
            _TERMINAL_ACCEPTANCE_KEY: {
                "version": 2,
                "stage": "accepted",
                "outcome": "completed",
                "queue_item_id": 1,
                "queue_started_at": LONG_AGO.isoformat(),
            }
        },
    )
    db_session.add(archive)
    await db_session.commit()
    archive_id = archive.id

    recover = AsyncMock(return_value=archive_id)
    with patch.object(print_reconciliation, "_recover_accepted_terminal_checkpoint", recover):
        closed = await print_reconciliation._reconcile(db_session, printer.id, "FINISH", "plate.gcode.3mf")
    assert closed == [archive_id]
    recover.assert_awaited_once()


# ── 5 · error_code holds only the closed vocabulary ──


def test_a_free_text_failure_reason_never_reaches_the_closed_error_code():
    free = "Nozzle clogged after the operator changed filament mid-print, see notes"
    assert failure_reasons.row_class("failed", free) == failure_reasons.OTHER
    assert failure_reasons.row_class("failed", failure_reasons.DISPATCH_UPLOAD_FAILED) == (
        failure_reasons.DISPATCH_UPLOAD_FAILED
    )


# ── 2 · a binding writer never waits for the journal lock while it may hold SQLite's writer ──


async def test_the_stock_insertion_claim_does_not_wait_for_the_journal_lock(db_session, printer_factory):
    from backend.app.services.auto_stock_spool import claim_on_insertion
    from backend.tests.unit.services.test_auto_stock_spool import event, manager, printer as stock_printer, spool

    p = await stock_printer(printer_factory)
    await spool(db_session)
    pm, _, _ = manager()
    async with journal.journal_lock(p.id):
        outcome = await asyncio.wait_for(claim_on_insertion(db_session, printer_id=p.id, event=event(), manager=pm), 5)
    assert outcome["reason"] == "assigned"


async def test_the_stock_insertion_handler_takes_the_journal_lock_before_its_session(printer_factory):
    from backend.app import main as main_module

    printer = await printer_factory()
    seen = []

    async def claim(db, *, printer_id, event, manager):
        seen.append(journal.journal_lock(printer_id).locked())
        return {"reason": "no_full_stock"}

    with patch("backend.app.services.auto_stock_spool.claim_on_insertion", claim):
        await main_module.on_stock_spool_inserted(
            printer.id, {"ams_id": 0, "tray_id": 0, "generation": 1, "sequence": 9_999}
        )
    assert seen == [True]


async def test_the_rfid_assignment_does_not_wait_for_the_journal_lock(db_session, printer_factory):
    from backend.app.services.spool_tag_matcher import auto_assign_spool, create_spool_from_tray
    from backend.tests.unit.services.test_spool_tag_matcher import SAMPLE_TRAY

    printer = await printer_factory()
    spool = await create_spool_from_tray(db_session, SAMPLE_TRAY)
    pm = MagicMock()
    pm.get_status.return_value = None
    pm.get_client.return_value = None
    pm.ensure_fresh_connection = AsyncMock(return_value=True)
    pm.ensure_fresh_connection_for_printer = AsyncMock(return_value=True)
    async with journal.journal_lock(printer.id):
        assignment = await asyncio.wait_for(auto_assign_spool(printer.id, 0, 0, spool, pm, db_session), 5)
    assert assignment.spool_id == spool.id


# ── 3 · an assignment after a return that still named the old reel is the new reel's ──


class Journal:
    def __init__(self):
        self.ids = count(1)
        self.events = []

    def add(self, event, layer, spool=None, kind=None):
        self.events.append(
            SimpleNamespace(
                id=next(self.ids),
                printer_id=10,
                archive_id=1006,
                event=event,
                kind=kind,
                global_tray_id=TRAY,
                layer_num=layer,
                spool_id=spool,
                spoolman_spool_id=spool + 1000 if spool else None,
                slot_occupied=True,
            )
        )
        return self


def boundaries(j):
    return [(layer, spool) for layer, spool, _ in journal_boundaries_for_tray(j.events, TRAY)]


def test_a_late_assignment_after_a_paused_tail_is_charged_from_the_return():
    """Review 3: tail → pause-runout → the reel swapped at the printer, feed back while BamDude
    still bound the old reel → the new reel assigned a few layers later."""
    j = (
        Journal()
        .add(EVENT_START, 0, spool=OLD)
        .add(EVENT_TAIL_START, 40, spool=OLD)
        .add(EVENT_RUNOUT, 42, spool=OLD, kind=KIND_PAUSE)
        .add(EVENT_FEED_RETURN, 42, spool=OLD)
        .add(EVENT_SPOOL_LOADED, 45, spool=NEW)
    )
    assert boundaries(j) == [(0, OLD), (42, NEW)]


def test_a_late_assignment_after_a_tail_without_a_pause_is_charged_from_the_return():
    j = (
        Journal()
        .add(EVENT_START, 0, spool=OLD)
        .add(EVENT_TAIL_START, 40, spool=OLD)
        .add(EVENT_FEED_RETURN, 45, spool=OLD)
        .add(EVENT_SPOOL_LOADED, 50, spool=NEW)
    )
    assert boundaries(j) == [(0, OLD), (45, NEW)]


async def test_an_assignment_after_a_same_reel_return_is_journaled(db_session, printer_factory):
    """Review 3: with no runout and no open tail, the late assignment wrote nothing at all."""
    from backend.tests.integration.test_tail_refill_journal import a_print, a_spool, bind, rows, tail_fact

    printer, archive = await a_print(db_session, printer_factory)
    old, new, other = await a_spool(db_session, "old"), await a_spool(db_session, "new"), await a_spool(db_session, "x")
    await bind(db_session, printer.id, old.id)
    await tail_fact(db_session, printer, "tail_start", 40)
    await tail_fact(db_session, printer, "feed_return", 45)

    async with journal.journal_lock(printer.id):
        for spool_id in (old.id, other.id, new.id):
            await journal.note_assignment_change(
                db_session, printer_id=printer.id, ams_id=0, tray_id=TRAY, spool_id=spool_id, layer_num=50
            )
    assert [r for r in await rows(db_session, printer.id, archive.id) if r[0] == EVENT_SPOOL_LOADED] == [
        (EVENT_SPOOL_LOADED, 50, new.id, None)
    ]


# ── Info · a dispatch of this process in flight is not "interrupted" ──


async def test_info_does_not_list_a_dispatch_this_process_is_running(
    db_session, printer_factory, archive_factory, monkeypatch
):
    from backend.app.services import print_state_recovery_store as store
    from backend.app.services.background_dispatch import background_dispatch
    from backend.app.services.farm_startup_guard import farm_startup_guard
    from backend.app.services.print_scheduler import scheduler
    from backend.app.services.printer_manager import printer_manager
    from backend.tests.integration.test_interrupted_dispatch import an_interrupted_job
    from backend.tests.unit.services.test_terminal_queue_repair import PrinterState

    monkeypatch.setattr(farm_startup_guard, "boot_id", lambda: "this-boot")
    state = PrinterState(connected=True, state="IDLE", connection_generation=1)
    monkeypatch.setattr(printer_manager, "peek_status", MagicMock(return_value=(state, 10.0, False)))
    monkeypatch.setattr(scheduler, "has_dispatch_in_flight", MagicMock(return_value=False))
    monkeypatch.setattr(background_dispatch, "has_work_for_printer", MagicMock(return_value=True))
    printer, queue, archive, item, _ = await an_interrupted_job(
        db_session, printer_factory, archive_factory, phase="sent", boot="this-boot"
    )
    page = await store.list_issues(db_session, None)
    assert not [i for i in page["issues"] if i["code"] == "interrupted_dispatch"]


# ── I · an interrupted direct print never starts on its own later ──


async def test_an_interrupted_direct_print_is_failed_not_requeued(
    db_session, printer_factory, archive_factory, monkeypatch
):
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.services import interrupted_dispatch
    from backend.app.services.background_dispatch import background_dispatch
    from backend.app.services.farm_startup_guard import farm_startup_guard
    from backend.app.services.print_scheduler import scheduler
    from backend.app.services.printer_manager import printer_manager
    from backend.tests.integration.test_interrupted_dispatch import an_interrupted_job
    from backend.tests.unit.services.test_terminal_queue_repair import PrinterState

    monkeypatch.setattr(farm_startup_guard, "boot_id", lambda: "this-boot")
    monkeypatch.setattr(farm_startup_guard, "allowed", lambda: True)
    state = PrinterState(connected=True, state="IDLE", connection_generation=1)
    monkeypatch.setattr(printer_manager, "peek_status", MagicMock(return_value=(state, 10.0, False)))
    monkeypatch.setattr(scheduler, "has_dispatch_in_flight", MagicMock(return_value=False))
    monkeypatch.setattr(background_dispatch, "has_work_for_printer", MagicMock(return_value=False))
    printer, queue, archive, item, _ = await an_interrupted_job(db_session, printer_factory, archive_factory)
    item.origin = "direct"
    await db_session.commit()
    item_id, queue_id = item.id, queue.id

    assert await interrupted_dispatch.recover_interrupted_dispatches(printer.id) == [interrupted_dispatch.FAILED]
    db_session.expire_all()
    status = await db_session.scalar(select(PrintQueueItem.status).where(PrintQueueItem.id == item_id))
    queue_status = await db_session.scalar(select(PrinterQueue.status).where(PrinterQueue.id == queue_id))
    assert status == "failed"
    assert queue_status == "idle"  # a direct print leaves the queue to the jobs behind it


# ── ruling · a terminal deferral fails its execution archive with the code ──


async def test_a_terminal_deferral_fails_its_execution_archive_with_the_code(db_session, printer_factory):
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.services.filament_deferred import defer_claim

    printer = await printer_factory()
    queue = PrinterQueue(printer_id=printer.id, status="printing")
    db_session.add(queue)
    await db_session.flush()
    from backend.tests.integration.test_interrupted_dispatch import NOT_STARTED

    archive = PrintArchive(
        printer_id=printer.id, queue_id=queue.id, filename="p.gcode.3mf", print_name="p",
        file_path="x/p.gcode.3mf", file_size=1, status="printing", dispatch_phase="prepared",
        extra_data=dict(NOT_STARTED),  # no swap start: the refusal alone ends the attempt
    )  # fmt: skip
    db_session.add(archive)
    await db_session.flush()
    started = datetime.now(timezone.utc)
    item = PrintQueueItem(queue_id=queue.id, archive_id=archive.id, status="printing", started_at=started)
    db_session.add(item)
    await db_session.commit()
    archive_id = archive.id

    await defer_claim(
        db_session,
        item_id=item.id,
        started_at=started,
        reason="printer_busy_at_start",
        revision=None,
        direct=True,
        execution_archive_id=archive_id,
    )
    await db_session.commit()
    db_session.expire_all()
    status, reason = (
        await db_session.execute(
            select(PrintArchive.status, PrintArchive.failure_reason).where(PrintArchive.id == archive_id)
        )
    ).one()
    assert status == "failed" and reason == failure_reasons.DISPATCH_START_REFUSED
