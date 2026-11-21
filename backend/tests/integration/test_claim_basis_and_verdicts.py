"""What the claim commits is what is dispatched, and what the pass ends is what it judged (G03 B, §A: A1-A5, A8, A9,
A14; acceptance TA1-TA6, TA12's column test, TA13-TA15).

The real scheduler and runners with device I/O faked (the harness of ``test_dispatch_without_original``), and, for
the previous-success gate, the real ``check_queue`` over one shared session (the harness of
``test_require_previous_success``). An edit "between the load and the claim" is made by wrapping the step the
pass awaits there and writing through a statement the pass's copies do not see.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import queue_row_basis
from backend.app.services.queue_counters import halt_lane
from backend.tests.integration.test_dispatch_attempt_outcome import a_queued_job, reread
from backend.tests.integration.test_dispatch_without_original import (  # noqa: F401 - fixtures
    clean_spool_state,
    dispatch_mocks,
    drain,
    scheduler_with_captured_dispatch,
    sessions,
)
from backend.tests.integration.test_require_previous_success import _finished, _printer_with_queue

pytestmark = pytest.mark.integration


def dispatched_options(spawned) -> dict | None:
    """The ``options`` the pass handed to its dispatch, read off the spawned coroutine before it runs."""
    for coroutine in spawned:
        if "_dispatch_and_finalize" in getattr(coroutine, "__qualname__", ""):
            return coroutine.cr_frame.f_locals["options"]
    return None


async def write(factory, item_id: int, **values) -> None:
    async with factory() as db:
        await db.execute(update(PrintQueueItem).where(PrintQueueItem.id == item_id).values(**values))
        await db.commit()


async def attempt_archives(db) -> int:
    db.expire_all()
    return await db.scalar(select(func.count()).select_from(PrintArchive).where(PrintArchive.status != "completed"))


@pytest.fixture
async def job(db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions):
    printer_id, queue_id, mqtt, item_id = await a_queued_job(
        db_session, tmp_path, printer_factory, monkeypatch, "library"
    )
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    dispatch_mocks(monkeypatch, factory)
    scheduler, spawned = scheduler_with_captured_dispatch(monkeypatch)
    return SimpleNamespace(
        printer_id=printer_id,
        queue_id=queue_id,
        mqtt=mqtt,
        item_id=item_id,
        factory=factory,
        scheduler=scheduler,
        spawned=spawned,
    )


def edit_before_the_claim(monkeypatch, job, **values) -> list:
    """The operator's edit lands while the pass awaits its claim — once."""
    import backend.app.services.print_scheduler as ps

    real = ps.PrintScheduler._claim_for_dispatch
    answers = []

    async def claim(self, **kwargs):
        if not answers:
            await write(job.factory, job.item_id, **values)
        result = await real(self, **kwargs)
        answers.append(result)
        return result

    monkeypatch.setattr(ps.PrintScheduler, "_claim_for_dispatch", claim)
    return answers


# ---------------------------------------------------------------- A1


def test_the_basis_is_every_column_but_the_scheduler_s_own_diagnostics():
    excluded = {"waiting_reason", "waiting_reason_code", "waiting_reason_checked_at", "updated_at"}
    assert excluded == queue_row_basis.EXCLUDED_COLUMNS
    row = PrintQueueItem(id=1, queue_id=2, status="pending", position=3, timelapse=False)
    columns = {c.key for c in PrintQueueItem.__table__.columns}
    assert set(queue_row_basis.basis_values(row)) == columns - excluded, "a new column is in the basis by default"
    before = queue_row_basis.row_basis(row)
    row.waiting_reason, row.waiting_reason_code = "Printer offline", "printer_offline"
    assert queue_row_basis.row_basis(row) == before
    row.timelapse = True
    assert queue_row_basis.row_basis(row) != before


def test_the_transfer_fences_with_the_same_definition():
    from backend.app.services import queue_auto_transfer

    row = PrintQueueItem(id=1, queue_id=2, status="pending", position=3)
    assert queue_auto_transfer._row_basis(row) == queue_row_basis.basis_values(row)


# ---------------------------------------------------------------- A3 / A5: the claim


async def test_an_edit_between_the_load_and_the_claim_is_not_dispatched_with_the_old_options(
    db_session, job, monkeypatch
):
    """TA1: refused ``item_changed`` — nothing written — and the next pass dispatches what the operator set."""
    answers = edit_before_the_claim(monkeypatch, job, bed_levelling_mode="on", layer_inspect=True)

    assert await job.scheduler.check_queue() is False
    assert [a.reason for a in answers] == ["item_changed"]
    row = await reread(db_session, PrintQueueItem, job.item_id)
    assert (row.status, row.dispatch_attempts or 0, row.started_at) == ("pending", 0, None)
    assert (await reread(db_session, PrinterQueue, job.queue_id)).status != "printing"
    assert await attempt_archives(db_session) == 0
    assert dispatched_options(job.spawned) is None

    assert await job.scheduler.check_queue() is True
    options = dispatched_options(job.spawned)
    assert (options["bed_levelling"], options["layer_inspect"]) == ("on", True)
    await drain(job.spawned)


async def test_a_plate_edited_mid_pass_is_not_reverted_by_the_claim(db_session, job, monkeypatch):
    """TA2: the claim used to write the pass's plate over the operator's."""
    edit_before_the_claim(monkeypatch, job, plate_id=7)

    assert await job.scheduler.check_queue() is False
    row = await reread(db_session, PrintQueueItem, job.item_id)
    assert (row.status, row.plate_id) == ("pending", 7)


async def test_an_untouched_row_is_claimed_on_its_first_pass_after_its_own_diagnostics(db_session, job):
    """TA3: the pass's own writes (a waiting reason cleared) never change the basis — no starvation."""
    await write(job.factory, job.item_id, waiting_reason="Staggered start", waiting_reason_code="stagger")

    assert await job.scheduler.check_queue() is True
    assert (await reread(db_session, PrintQueueItem, job.item_id)).status == "printing"
    await drain(job.spawned)


async def test_the_dispatch_is_built_from_the_claimed_row_not_the_tick_s_copy(db_session, job, monkeypatch):
    """TA6 / A5: the tick's copy changed after a successful claim; the dispatched options do not move."""
    from sqlalchemy.orm.attributes import set_committed_value

    import backend.app.services.print_scheduler as ps

    copies = []
    real_preflight = ps.preflight_item

    async def preflight(db, item, *args, **kwargs):
        copies.append(item)
        return await real_preflight(db, item, *args, **kwargs)

    real_claim = ps.PrintScheduler._claim_for_dispatch

    async def claim(self, **kwargs):
        result = await real_claim(self, **kwargs)
        for copy in copies:
            set_committed_value(copy, "bed_levelling_mode", "on")
            set_committed_value(copy, "layer_inspect", True)
            set_committed_value(copy, "created_by_id", 999)
        return result

    monkeypatch.setattr(ps, "preflight_item", preflight)
    monkeypatch.setattr(ps.PrintScheduler, "_claim_for_dispatch", claim)

    assert await job.scheduler.check_queue() is True
    options = dispatched_options(job.spawned)
    row = await reread(db_session, PrintQueueItem, job.item_id)
    assert options["layer_inspect"] == bool(row.layer_inspect) and options["layer_inspect"] is not True
    assert options["bed_levelling"] != "on" or row.bed_levelling_mode == "on"
    coroutine = next(c for c in job.spawned if "_dispatch_and_finalize" in c.__qualname__)
    assert coroutine.cr_frame.f_locals["requested_by_user_id"] == row.created_by_id
    await drain(job.spawned)


async def test_a_row_moved_to_another_queue_mid_pass_is_not_claimed_for_the_old_printer(
    db_session, job, printer_factory, monkeypatch
):
    """A4: the CAS names the lane; the basis already refuses a moved row."""
    other = await printer_factory()
    other_id = other.id
    db_session.add(PrinterQueue(id=other_id, printer_id=other_id))
    await db_session.commit()
    answers = edit_before_the_claim(monkeypatch, job, queue_id=other_id)

    assert await job.scheduler.check_queue() is False
    assert [a.reason for a in answers] == ["not_first_runnable"]
    row = await reread(db_session, PrintQueueItem, job.item_id)
    assert (row.status, row.queue_id) == ("pending", other_id)
    assert (await reread(db_session, PrinterQueue, job.queue_id)).status != "printing"


# ---------------------------------------------------------------- A8: V3 (_fail_item)


def refuse_the_connection(monkeypatch):
    import backend.app.services.print_scheduler as ps

    monkeypatch.setattr(ps.printer_manager, "ensure_fresh_connection_for_printer", AsyncMock(return_value=False))


def before_the_failure(monkeypatch, action):
    import backend.app.services.print_scheduler as ps

    real = ps.PrintScheduler._fail_item
    outcomes = []

    async def fail(self, db, item, *args, **kwargs):
        await action()
        outcome = await real(self, db, item, *args, **kwargs)
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(ps.PrintScheduler, "_fail_item", fail)
    return outcomes


async def test_an_untouched_row_refused_before_the_claim_fails_and_halts_its_queue(db_session, job, monkeypatch):
    """TA3 (V3 written on its first pass) and A9: the halt is ``halt_lane``'s — no live claim, so an error."""
    refuse_the_connection(monkeypatch)
    outcomes = before_the_failure(monkeypatch, AsyncMock())

    await job.scheduler.check_queue()
    assert [o.written for o in outcomes] == [True]
    row = await reread(db_session, PrintQueueItem, job.item_id)
    assert (row.status, row.gate_acknowledged) == ("failed", True)
    assert (await reread(db_session, PrinterQueue, job.queue_id)).status == "error"


@pytest.mark.parametrize("change", ["cancelled", "edited", "deleted"])
async def test_a_row_changed_before_its_failure_is_not_failed(db_session, job, monkeypatch, change):
    """TA4: cancelled / edited in place / deleted mid-pass — no failure, no halt, no StaleDataError."""
    refuse_the_connection(monkeypatch)

    async def action():
        async with job.factory() as db:
            if change == "deleted":
                await db.execute(PrintQueueItem.__table__.delete().where(PrintQueueItem.id == job.item_id))
            elif change == "cancelled":
                await db.execute(
                    update(PrintQueueItem).where(PrintQueueItem.id == job.item_id).values(status="cancelled")
                )
            else:
                await db.execute(
                    update(PrintQueueItem).where(PrintQueueItem.id == job.item_id).values(layer_inspect=True)
                )
            await db.commit()

    outcomes = before_the_failure(monkeypatch, action)
    await job.scheduler.check_queue()

    # A row no longer pending is refused already where the head is asked; an edited one where it is locked.
    expected = {"cancelled": "not_first_runnable", "edited": "item_changed", "deleted": "not_first_runnable"}[change]
    assert [(o.written, o.code) for o in outcomes] == [(False, expected)]
    row = await reread(db_session, PrintQueueItem, job.item_id)
    if change == "deleted":
        assert row is None
    else:
        assert row.status == ("cancelled" if change == "cancelled" else "pending")
    assert (await reread(db_session, PrinterQueue, job.queue_id)).status != "error"


async def test_a_direct_print_that_claimed_the_printer_meanwhile_keeps_its_claim(db_session, job, monkeypatch):
    """TA5 (R4): the verdict asks the lane's admission first; the row stays pending, the header untouched."""
    refuse_the_connection(monkeypatch)

    async def direct_claim():
        async with job.factory() as db:
            await db.execute(
                update(PrinterQueue)
                .where(PrinterQueue.id == job.queue_id)
                .values(status="printing", current_item_id=None)
            )
            await db.commit()

    outcomes = before_the_failure(monkeypatch, direct_claim)
    await job.scheduler.check_queue()

    assert [(o.written, o.code) for o in outcomes] == [(False, "active_claim")]
    assert (await reread(db_session, PrintQueueItem, job.item_id)).status == "pending"
    header = await reread(db_session, PrinterQueue, job.queue_id)
    assert (header.status, header.current_item_id, header.is_paused) == ("printing", None, False)


# ---------------------------------------------------------------- A8: V1 (the previous-success gate)


@pytest.fixture
def gate_scheduler(monkeypatch, db_session):
    from backend.app.services.print_scheduler import PrintScheduler

    @asynccontextmanager
    async def shared():
        yield db_session

    monkeypatch.setattr("backend.app.services.print_scheduler.async_session", shared)
    return PrintScheduler()


def idle_printer():
    return patch.multiple(
        "backend.app.services.print_scheduler.printer_manager",
        is_connected=lambda pid: True,
        get_status=lambda pid: SimpleNamespace(state="IDLE", raw_data={}),
        is_awaiting_plate_clear=lambda pid: False,
    )


async def gated_queue(db_session, printer_factory, **head):
    _printer, queue = await _printer_with_queue(db_session, printer_factory)
    failure = await _finished(db_session, queue, "failed", minutes_ago=10)
    item = PrintQueueItem(queue_id=queue.id, status="pending", position=1, require_previous_success=True, **head)
    db_session.add(item)
    await db_session.commit()
    return queue, failure, item


def at_the_gate(monkeypatch, scheduler, action):
    """``action`` runs once, after the pass read the gate shut and before the verdict proves it."""
    real = type(scheduler).previous_print_succeeded
    calls = []

    async def gate(self, db, printer_id, exclude_item_id=None):
        answer = await real(self, db, printer_id, exclude_item_id)
        if not calls:
            calls.append(answer)
            await action(db)
        return answer

    monkeypatch.setattr(type(scheduler), "previous_print_succeeded", gate)


async def run_pass(scheduler):
    sent = AsyncMock()
    with patch("backend.app.services.print_scheduler.notification_service.on_queue_job_skipped", sent), idle_printer():
        await scheduler.check_queue()
    return sent


async def test_an_untouched_gated_head_is_skipped_on_its_first_pass(db_session, printer_factory, gate_scheduler):
    _queue, _failure, item = await gated_queue(db_session, printer_factory)
    sent = await run_pass(gate_scheduler)
    await db_session.refresh(item)
    assert item.status == "skipped" and sent.await_count == 1


async def test_a_row_moved_before_the_gated_head_is_judged_first_and_the_head_is_not_skipped(
    db_session, printer_factory, gate_scheduler, monkeypatch
):
    """TA13 (R1): the operator moves B before A between the gate's read and the skip."""
    queue, _failure, item = await gated_queue(db_session, printer_factory)
    other = PrintQueueItem(queue_id=queue.id, status="pending", position=5)
    db_session.add(other)
    await db_session.commit()

    async def move(db):
        await db.execute(
            update(PrintQueueItem)
            .where(PrintQueueItem.id == other.id)
            .values(position=0)
            .execution_options(synchronize_session=False)
        )

    at_the_gate(monkeypatch, gate_scheduler, move)
    sent = await run_pass(gate_scheduler)
    status = await db_session.scalar(select(PrintQueueItem.status).where(PrintQueueItem.id == item.id))
    assert status == "pending" and sent.await_count == 0


async def test_a_row_ahead_that_becomes_runnable_keeps_the_head_unjudged(
    db_session, printer_factory, gate_scheduler, monkeypatch
):
    """TA14 (R1): a staged row ahead of the judged head has its manual start cleared meanwhile."""
    queue, _failure, item = await gated_queue(db_session, printer_factory)
    staged = PrintQueueItem(queue_id=queue.id, status="pending", position=0, manual_start=True)
    db_session.add(staged)
    await db_session.commit()

    async def start(db):
        await db.execute(
            update(PrintQueueItem)
            .where(PrintQueueItem.id == staged.id)
            .values(manual_start=False)
            .execution_options(synchronize_session=False)
        )

    at_the_gate(monkeypatch, gate_scheduler, start)
    sent = await run_pass(gate_scheduler)
    status = await db_session.scalar(select(PrintQueueItem.status).where(PrintQueueItem.id == item.id))
    assert status == "pending" and sent.await_count == 0


async def test_an_acknowledgement_between_the_gate_and_the_skip_lets_the_head_through(
    db_session, printer_factory, gate_scheduler, monkeypatch
):
    """TA15 (R1): the gate is asked again in the verdict's transaction — ``gate_passes``."""
    _queue, failure, item = await gated_queue(db_session, printer_factory)

    async def acknowledge(db):
        await db.execute(
            update(PrintQueueItem)
            .where(PrintQueueItem.id == failure.id)
            .values(gate_acknowledged=True)
            .execution_options(synchronize_session=False)
        )

    at_the_gate(monkeypatch, gate_scheduler, acknowledge)
    sent = await run_pass(gate_scheduler)
    status = await db_session.scalar(select(PrintQueueItem.status).where(PrintQueueItem.id == item.id))
    assert status != "skipped" and sent.await_count == 0


# ---------------------------------------------------------------- A14 / V4: the timelapse pause


async def test_only_the_head_pauses_its_queue_for_a_full_timelapse_medium(
    db_session, printer_factory, gate_scheduler, monkeypatch
):
    """A14: a staged or later row wanting a timelapse never pauses the queue; the head does, through V4."""
    import backend.app.services.print_scheduler as ps

    _printer, queue = await _printer_with_queue(db_session, printer_factory)
    staged = PrintQueueItem(queue_id=queue.id, status="pending", position=0, manual_start=True, timelapse=True)
    db_session.add(staged)
    await db_session.commit()
    monkeypatch.setattr(ps, "_timelapse_storage_full", lambda _pid: True)

    await run_pass(gate_scheduler)
    assert await db_session.scalar(select(PrinterQueue.is_paused).where(PrinterQueue.id == queue.id)) is False

    head = PrintQueueItem(queue_id=queue.id, status="pending", position=1, timelapse=True)
    db_session.add(head)
    await db_session.commit()
    await run_pass(gate_scheduler)
    assert await db_session.scalar(select(PrinterQueue.is_paused).where(PrinterQueue.id == queue.id)) is True
    reason = await db_session.scalar(select(PrintQueueItem.waiting_reason_code).where(PrintQueueItem.id == head.id))
    assert reason == "storage_full"


async def test_a_head_whose_timelapse_was_unticked_meanwhile_does_not_pause_its_queue(
    db_session, printer_factory, gate_scheduler, monkeypatch
):
    """TA15: the row is not the one judged any more — ``item_changed``, no pause."""
    import backend.app.services.print_scheduler as ps

    _printer, queue = await _printer_with_queue(db_session, printer_factory)
    head = PrintQueueItem(queue_id=queue.id, status="pending", position=1, timelapse=True)
    db_session.add(head)
    await db_session.commit()
    calls = []

    def full(_pid):
        if not calls:
            calls.append(1)
            return True
        return True

    real_verdict = ps.PrintScheduler._verdict_storage_full

    async def verdict(self, db, item, basis, printer_id):
        await db.execute(
            update(PrintQueueItem)
            .where(PrintQueueItem.id == head.id)
            .values(timelapse=False)
            .execution_options(synchronize_session=False)
        )
        return await real_verdict(self, db, item, basis, printer_id)

    monkeypatch.setattr(ps, "_timelapse_storage_full", full)
    monkeypatch.setattr(ps.PrintScheduler, "_verdict_storage_full", verdict)
    await run_pass(gate_scheduler)
    assert await db_session.scalar(select(PrinterQueue.is_paused).where(PrinterQueue.id == queue.id)) is False


# ---------------------------------------------------------------- A9: halt_lane


async def header(db_session, printer_factory, **values):
    _printer, queue = await _printer_with_queue(db_session, printer_factory)
    for key, value in values.items():
        setattr(queue, key, value)
    await db_session.commit()
    return queue


async def test_halt_lane_halts_a_queue_whose_header_names_the_item(db_session, printer_factory):
    queue = await header(db_session, printer_factory, status="printing")
    item = PrintQueueItem(queue_id=queue.id, status="failed", position=1)
    db_session.add(item)
    await db_session.flush()
    queue.current_item_id = item.id
    await db_session.commit()
    assert await halt_lane(db_session, queue.id, item_id=item.id, kind="error") == "halted"
    await db_session.commit()
    row = await reread(db_session, PrinterQueue, queue.id)
    assert (row.status, row.current_item_id, row.is_paused) == ("error", None, False)


async def test_halt_lane_only_pauses_a_queue_another_run_holds(db_session, printer_factory):
    queue = await header(db_session, printer_factory, status="printing")
    theirs = PrintQueueItem(queue_id=queue.id, status="printing", position=0)
    mine = PrintQueueItem(queue_id=queue.id, status="failed", position=1)
    db_session.add_all([theirs, mine])
    await db_session.flush()
    queue.current_item_id = theirs.id
    await db_session.commit()
    theirs_id, mine_id, queue_id = theirs.id, mine.id, queue.id
    assert await halt_lane(db_session, queue_id, item_id=mine_id, kind="error") == "paused_only"
    await db_session.commit()
    row = await reread(db_session, PrinterQueue, queue_id)
    assert (row.status, row.current_item_id, row.is_paused) == ("printing", theirs_id, True)


async def test_halt_lane_halts_a_lane_without_a_live_claim(db_session, printer_factory):
    queue = await header(db_session, printer_factory, status="idle")
    assert await halt_lane(db_session, queue.id, item_id=None, kind="paused") == "halted"
    await db_session.commit()
    assert (await reread(db_session, PrinterQueue, queue.id)).status == "paused"


@pytest.mark.parametrize(
    ("printing_row", "moved", "expected"),
    [(False, False, "halted"), (True, False, "paused_only"), (False, True, "unchanged")],
)
async def test_halt_lane_addressed_form_for_a_stale_header(db_session, printer_factory, printing_row, moved, expected):
    queue = await header(db_session, printer_factory, status="printing")
    if printing_row:
        db_session.add(PrintQueueItem(queue_id=queue.id, status="printing", position=0))
    if moved:
        queue.status = "idle"
    await db_session.commit()
    outcome = await halt_lane(db_session, queue.id, item_id=None, kind="error", stale_only=True, read_item_id=None)
    await db_session.commit()
    assert outcome == expected
    row = await reread(db_session, PrinterQueue, queue.id)
    assert row.status == {"halted": "error", "paused_only": "printing", "unchanged": "idle"}[expected]
    assert row.is_paused is (expected == "paused_only")


async def test_halt_lane_refuses_an_unknown_kind(db_session, printer_factory):
    queue = await header(db_session, printer_factory)
    with pytest.raises(ValueError):
        await halt_lane(db_session, queue.id, item_id=None, kind="idle")
