"""How a dispatch attempt ends: the printer's answers, the swap-start veto, the phase and the budget.

Spec: vault ``60-specs/post-rebuild-dispatch-transactions-spec.md`` — D-R3 (phase),
D-R4 (three negative answers, one veto for every automatic pending), D-R5 (the
budget is spent at the claim, given back only by a typed refusal before publish,
opened again by the operator's Retry), acceptance rows D05–D08. The real scheduler
and runners, device I/O faked (the harness of ``test_dispatch_without_original``).
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401
from backend.tests.integration.test_dispatch_attempt_outcome import a_queued_job, execution_archive, reread
from backend.tests.integration.test_dispatch_without_original import (  # noqa: F401 - fixtures
    clean_spool_state,
    dispatch_mocks,
    drain,
    scheduler_with_captured_dispatch,
    sessions,
)

pytestmark = pytest.mark.integration


async def a_job_and_its_dispatch(db_session, test_engine, tmp_path, printer_factory, monkeypatch, kind="library"):
    printer_id, queue_id, mqtt, item_id = await a_queued_job(db_session, tmp_path, printer_factory, monkeypatch, kind)
    service = dispatch_mocks(monkeypatch, async_sessionmaker(test_engine, expire_on_commit=False))
    scheduler, spawned = scheduler_with_captured_dispatch(monkeypatch)
    return queue_id, mqtt, item_id, service, scheduler, spawned


async def one_tick(scheduler, spawned):
    await scheduler.check_queue()
    await drain(spawned)


# --------------------------------------------------------------------------- #
# D05 — the printer's three negative answers end the attempt with their class
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        ("no_acknowledgement", "dispatchNoAcknowledgement"),
        ("file_received", "dispatchAcceptedNotStarted"),
        ("stalled_after_accept", "dispatchAcceptedNotStarted"),
    ],
)
@pytest.mark.parametrize("kind", ["library", "archive"])
async def test_a_negative_answer_fails_the_attempt_with_its_class_and_halts_the_queue(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions, kind, answer, code
):
    queue_id, mqtt, item_id, service, scheduler, spawned = await a_job_and_its_dispatch(
        db_session, test_engine, tmp_path, printer_factory, monkeypatch, kind
    )
    monkeypatch.setattr(service, "_await_print_response", AsyncMock(return_value=answer))
    run = service.run_from_queue_item
    outcomes = []

    async def record_outcome(**kwargs):
        outcome = await run(**kwargs)
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(service, "run_from_queue_item", record_outcome)

    await one_tick(scheduler, spawned)

    mqtt._client.publish.assert_called_once()
    row = await reread(db_session, PrintQueueItem, item_id)
    assert row.status == "failed" and row.error_code == code, row.error_message
    assert row.dispatch_attempts == 1  # the claim spent it; a published start gives nothing back
    status, _message, _extra = await execution_archive(db_session)
    assert status == "failed"
    archive = (await db_session.scalars(select(PrintArchive).where(PrintArchive.status == "failed"))).one()
    assert archive.failure_reason == code
    assert len(outcomes) == 1
    assert outcomes[0]["archive_terminal"] == "written"
    assert outcomes[0]["execution_archive_id"] == archive.id
    assert outcomes[0]["execution_archive_identity"] == archive.recovery_identity
    assert (await db_session.get(PrinterQueue, queue_id)).status == "error"

    # Never sent twice by itself: the halted queue waits for the operator.
    await one_tick(scheduler, spawned)
    mqtt._client.publish.assert_called_once()


async def test_no_ack_on_a_swap_dispatch_keeps_the_execution_owned(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions
):
    import backend.app.services.background_dispatch as bd
    from backend.app.services import swap_compatibility
    from backend.tests.integration.test_dispatch_attempt_outcome import queued
    from backend.tests.integration.test_dispatch_without_original import a_library_source
    from backend.tests.integration.test_filament_routing_dispatch import setup_source

    _, printer, queue, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    printer.swap_mode_enabled = True
    source = await a_library_source(db_session, tmp_path, name="lamp.gcode.3mf")
    swap_compatibility.declare_ingest(source, False, origin="classifier")
    await db_session.commit()
    item = await queued(db_session, queue, source, "library")
    item.execute_swap_macros = True
    item.swap_macro_events = '["swap_mode_change_table"]'
    item_id, queue_id = item.id, queue.id
    await db_session.commit()
    service = dispatch_mocks(monkeypatch, async_sessionmaker(test_engine, expire_on_commit=False))
    scheduler, spawned = scheduler_with_captured_dispatch(monkeypatch)
    monkeypatch.setattr(service, "_await_print_response", AsyncMock(return_value=bd.RESPONSE_NO_ACKNOWLEDGEMENT))

    await one_tick(scheduler, spawned)
    row = await reread(db_session, PrintQueueItem, item_id)
    archive = await db_session.get(PrintArchive, row.archive_id)
    assert row.status == archive.status == "printing"
    assert archive.completed_at is None and archive.dispatch_phase == "sent"
    assert archive.extra_data["swap_macro_events_pending"] == ["swap_mode_change_table"]
    queue = await db_session.get(PrinterQueue, queue_id)
    assert queue.status == "printing" and queue.current_item_id == item_id
    assert bd.attempt_unobserved(archive.id)
    await one_tick(scheduler, spawned)
    mqtt._client.publish.assert_called_once()


# --------------------------------------------------------------------------- #
# D05 — one veto for every automatic pending: a swap start that ran
# --------------------------------------------------------------------------- #


async def test_a_refusal_after_the_swap_start_ran_fails_instead_of_waiting(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions
):
    import backend.app.services.background_dispatch as bd
    from backend.app.services.filament_routing import RoutingDeferred

    queue_id, mqtt, item_id, service, scheduler, spawned = await a_job_and_its_dispatch(
        db_session, test_engine, tmp_path, printer_factory, monkeypatch
    )

    async def the_start_macro_ran(job, printer, event, message, *, record=None):
        if event == "swap_mode_start":
            job.swap_start_effect = "fired"
            if record is not None:
                await record("fired")

    monkeypatch.setattr(service, "_run_swap_macro_if_needed", the_start_macro_ran)
    monkeypatch.setattr(bd, "settle_plan", AsyncMock(side_effect=RoutingDeferred("feed_settle_timeout")))

    await one_tick(scheduler, spawned)

    mqtt._client.publish.assert_not_called()
    row = await reread(db_session, PrintQueueItem, item_id)
    assert row.status == "failed" and row.error_code == "dispatchStartRefused"
    assert "swap start macro" in row.error_message
    assert row.dispatch_attempts == 1  # no attempt is given back over a physical effect
    status, _message, _extra = await execution_archive(db_session)
    assert status == "failed"
    assert (await db_session.get(PrinterQueue, queue_id)).status == "error"


async def test_the_same_refusal_without_a_swap_start_waits_and_returns_its_attempt(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions
):
    import backend.app.services.background_dispatch as bd
    from backend.app.services.filament_routing import RoutingDeferred

    queue_id, mqtt, item_id, _service, scheduler, spawned = await a_job_and_its_dispatch(
        db_session, test_engine, tmp_path, printer_factory, monkeypatch
    )
    monkeypatch.setattr(bd, "settle_plan", AsyncMock(side_effect=RoutingDeferred("feed_settle_timeout")))

    await one_tick(scheduler, spawned)

    row = await reread(db_session, PrintQueueItem, item_id)
    assert row.status == "pending" and row.dispatch_attempts == 0 and row.error_code is None
    assert row.started_at is None  # a proven no-effect deferral gives the whole claim back
    status, _message, extra = await execution_archive(db_session)
    assert status == "cancelled" and extra["dispatch_aborted"] is True
    assert (await db_session.get(PrinterQueue, queue_id)).status == "idle"


async def _retry(db_session, queue_id, item_id, how):
    from backend.app.api.routes.print_queue import retry_failed_item
    from backend.app.models.user import User
    from backend.app.services.queue_bulk_retry import retry_block
    from backend.app.services.swap_source_declarations import Actor

    if how == "single":
        await retry_failed_item(item_id, db=db_session, _=None)
        return
    user = User(username="retrying-operator", password_hash="unused", role="admin", is_active=True)
    db_session.add(user)
    await db_session.commit()
    result = await retry_block(
        db_session, queue_id, [item_id], actor=Actor("user", user.id, user.username), granted_scope=None, key_hash=None
    )
    assert result["retried"] == [item_id], result


def _refresh_refusal(row):
    """What a swap-declaration refresh answers for this job, asked by an operator who sees and edits every job."""
    from backend.app.services import swap_job_refresh
    from backend.app.services.swap_source_declarations import Actor

    return swap_job_refresh._refusal(
        "queue",
        row,
        source_kind="library",
        source_id=row.library_file_id or 0,
        sha256="0" * 64,
        blob=None,
        actor=Actor("user", None, "operator"),
        rights=swap_job_refresh.JobRights(read_all=True, update_all=True),
        scope=None,
    )


@pytest.mark.parametrize("how", ["single", "bulk"])
@pytest.mark.parametrize("effect", ["fired", "possibly_fired"])
@pytest.mark.parametrize("kind", ["library", "archive"])
async def test_a_swap_start_refusal_keeps_the_proof_of_dispatch_through_a_retry(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions, kind, effect, how
):
    """A swap start that ran, or may have, makes the attempt a dispatched one: the deferral
    ends the row without erasing ``started_at``, and after a Retry a swap-declaration
    refresh still refuses the job as dispatched before."""
    import backend.app.services.background_dispatch as bd
    from backend.app.services.filament_routing import RoutingDeferred

    queue_id, mqtt, item_id, service, scheduler, spawned = await a_job_and_its_dispatch(
        db_session, test_engine, tmp_path, printer_factory, monkeypatch, kind
    )

    async def the_start_macro(job, printer, event, message, *, record=None):
        if event == "swap_mode_start":
            job.swap_start_effect = effect
            if record is not None:
                await record(effect)

    monkeypatch.setattr(service, "_run_swap_macro_if_needed", the_start_macro)
    monkeypatch.setattr(bd, "settle_plan", AsyncMock(side_effect=RoutingDeferred("feed_settle_timeout")))

    await one_tick(scheduler, spawned)

    mqtt._client.publish.assert_not_called()
    row = await reread(db_session, PrintQueueItem, item_id)
    assert row.status == "failed" and row.started_at is not None
    claimed_at = row.started_at

    await _retry(db_session, queue_id, item_id, how)

    row = await reread(db_session, PrintQueueItem, item_id)
    assert row.status == "pending" and row.dispatch_attempts == 0
    assert row.started_at == claimed_at
    assert _refresh_refusal(row) == "not_before_dispatch"


async def test_a_terminal_refusal_without_a_swap_start_keeps_its_claim_time(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions
):
    """A refusal that ENDS the row keeps ``started_at`` too: only a no-effect deferral that
    sends the row back to pending clears it."""
    import backend.app.services.background_dispatch as bd
    from backend.app.services.filament_deferred import DISPOSITION_FAILED, SourceCaptureDeferred

    _queue_id, mqtt, item_id, _service, scheduler, spawned = await a_job_and_its_dispatch(
        db_session, test_engine, tmp_path, printer_factory, monkeypatch
    )
    refusal = SourceCaptureDeferred("source_timeout", disposition=DISPOSITION_FAILED)
    monkeypatch.setattr(bd, "settle_plan", AsyncMock(side_effect=refusal))

    await one_tick(scheduler, spawned)

    mqtt._client.publish.assert_not_called()
    row = await reread(db_session, PrintQueueItem, item_id)
    assert row.status == "failed" and row.started_at is not None


# --------------------------------------------------------------------------- #
# D-R4 — the observer after the send: never dropped by its own failure
# --------------------------------------------------------------------------- #


@pytest.fixture
def quick_observer(monkeypatch):
    import backend.app.services.background_dispatch as bd

    monkeypatch.setattr(bd, "OBSERVER_RETRY_SECONDS", 0, raising=False)
    monkeypatch.setattr(bd, "OBSERVER_WATCH_SECONDS", 0, raising=False)


async def test_a_watch_that_fails_fast_still_lasts_the_printers_own_answer_window(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions
):
    """Review r1: looks that fail at once would end the watch in seconds, long before a printer
    still reading the file (#1150) answers — the watch lasts at least the observer's own window,
    so Info never offers the close of an attempt the printer may be about to start."""
    import time

    import backend.app.services.background_dispatch as bd

    monkeypatch.setattr(bd, "OBSERVER_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(bd, "OBSERVER_WATCH_SECONDS", 0.3)
    _queue_id, mqtt, _item_id, service, scheduler, spawned = await a_job_and_its_dispatch(
        db_session, test_engine, tmp_path, printer_factory, monkeypatch
    )
    looks = []

    async def failing(*_args, **_kwargs):
        looks.append(time.monotonic())
        raise RuntimeError("the status read raised")

    monkeypatch.setattr(service, "_await_print_response", failing)

    await one_tick(scheduler, spawned)

    mqtt._client.publish.assert_called_once()
    assert len(looks) > bd.OBSERVER_MAX_ATTEMPTS
    assert looks[-1] - looks[0] >= 0.3 - 0.02


@pytest.mark.parametrize("glitch", [RuntimeError("the status read raised"), "running"], ids=["exception", "no_verdict"])
async def test_a_transient_observer_failure_keeps_watching_until_the_printer_answers(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions, quick_observer, glitch
):
    """The first look after the send fails (an exception, or an answer that is no verdict of
    the printer's); the runner looks again with the same claim, and the printer's answer ends
    the attempt — sent once, never left unwatched."""
    import backend.app.services.background_dispatch as bd

    _queue_id, mqtt, item_id, service, scheduler, spawned = await a_job_and_its_dispatch(
        db_session, test_engine, tmp_path, printer_factory, monkeypatch
    )
    verdict = AsyncMock(side_effect=[glitch, bd.RESPONSE_ACCEPTED])
    monkeypatch.setattr(service, "_await_print_response", verdict)

    await one_tick(scheduler, spawned)

    mqtt._client.publish.assert_called_once()
    assert verdict.await_count == 2
    row = await reread(db_session, PrintQueueItem, item_id)
    archive = await db_session.get(PrintArchive, row.archive_id)
    assert row.status == "printing" and archive.dispatch_phase == "accepted"


async def test_a_transient_observer_failure_then_a_negative_answer_ends_the_attempt_with_its_class(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions, quick_observer
):
    import backend.app.services.background_dispatch as bd

    queue_id, mqtt, item_id, service, scheduler, spawned = await a_job_and_its_dispatch(
        db_session, test_engine, tmp_path, printer_factory, monkeypatch
    )
    verdict = AsyncMock(side_effect=[RuntimeError("the status read raised"), bd.RESPONSE_NO_ACKNOWLEDGEMENT])
    monkeypatch.setattr(service, "_await_print_response", verdict)

    await one_tick(scheduler, spawned)

    mqtt._client.publish.assert_called_once()
    row = await reread(db_session, PrintQueueItem, item_id)
    assert row.status == "failed" and row.error_code == "dispatchNoAcknowledgement"
    assert (await db_session.get(PrinterQueue, queue_id)).status == "error"


async def test_a_persistent_observer_failure_ends_the_watch_visibly_and_keeps_the_claim(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions, quick_observer
):
    """Every look fails: the watch ends after a bounded number of tries, the start is not
    sent again, the claim and the archive stay as the printer may have them, and this
    process records that nothing watches the attempt any more (Info offers its close)."""
    import backend.app.services.background_dispatch as bd

    queue_id, mqtt, item_id, service, scheduler, spawned = await a_job_and_its_dispatch(
        db_session, test_engine, tmp_path, printer_factory, monkeypatch
    )
    verdict = AsyncMock(side_effect=RuntimeError("the observer keeps failing"))
    monkeypatch.setattr(service, "_await_print_response", verdict)

    await one_tick(scheduler, spawned)

    mqtt._client.publish.assert_called_once()
    assert verdict.await_count == bd.OBSERVER_MAX_ATTEMPTS
    row = await reread(db_session, PrintQueueItem, item_id)
    archive = await db_session.get(PrintArchive, row.archive_id)
    assert row.status == "printing" and archive.status == "printing" and archive.dispatch_phase == "sent"
    assert (await db_session.get(PrinterQueue, queue_id)).status == "printing"
    assert bd.attempt_unobserved(archive.id)
    assert not service._active_jobs

    await one_tick(scheduler, spawned)
    mqtt._client.publish.assert_called_once()  # the claim holds: never sent again by itself


async def test_a_new_attempt_on_a_reused_archive_id_is_not_taken_for_an_unwatched_one(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions
):
    """SQLite hands a deleted newest id out again: the new attempt that gets it is watched."""
    from sqlalchemy import func

    import backend.app.services.background_dispatch as bd

    _queue_id, _mqtt, item_id, _service, scheduler, spawned = await a_job_and_its_dispatch(
        db_session, test_engine, tmp_path, printer_factory, monkeypatch
    )
    next_id = (await db_session.scalar(select(func.max(PrintArchive.id))) or 0) + 1
    bd.mark_unobserved(next_id)

    await one_tick(scheduler, spawned)

    row = await reread(db_session, PrintQueueItem, item_id)
    assert row.archive_id == next_id
    assert not bd.attempt_unobserved(next_id)


# --------------------------------------------------------------------------- #
# D07 — the phase: a ``sending`` mark that cannot be written forbids the send
# --------------------------------------------------------------------------- #


async def test_a_lost_sending_mark_sends_nothing(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions
):
    import backend.app.services.background_dispatch as bd
    from backend.app.services import dispatch_phase

    _queue_id, _mqtt, item_id, _service, scheduler, spawned = await a_job_and_its_dispatch(
        db_session, test_engine, tmp_path, printer_factory, monkeypatch
    )
    real_advance = dispatch_phase.advance

    async def advance(db, archive_id, to, *, boot_id, planned_color_changes=None):
        if to == dispatch_phase.SENDING:
            return False
        return await real_advance(db, archive_id, to, boot_id=boot_id)

    monkeypatch.setattr(bd.dispatch_phase, "advance", advance)
    start = MagicMock(return_value=True)
    monkeypatch.setattr(bd.printer_manager, "start_print", start)

    await one_tick(scheduler, spawned)

    start.assert_not_called()
    row = await reread(db_session, PrintQueueItem, item_id)
    assert row.status == "failed" and row.error_code == "dispatchInternalError"


async def test_the_phase_follows_the_attempt_to_accepted(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions
):
    _queue_id, _mqtt, _item_id, _service, scheduler, spawned = await a_job_and_its_dispatch(
        db_session, test_engine, tmp_path, printer_factory, monkeypatch
    )

    await one_tick(scheduler, spawned)

    db_session.expire_all()
    phase = (
        await db_session.execute(select(PrintArchive.dispatch_phase).where(PrintArchive.status == "printing"))
    ).scalar_one()
    assert phase == "accepted"


# --------------------------------------------------------------------------- #
# D08 — the budget: spent at the claim, given back by a typed refusal, opened by Retry
# --------------------------------------------------------------------------- #


async def test_repeated_refusals_never_exhaust_the_budget(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions
):
    import backend.app.services.background_dispatch as bd
    from backend.app.services.print_scheduler import DISPATCH_MAX_ATTEMPTS

    _queue_id, _mqtt, item_id, _service, scheduler, spawned = await a_job_and_its_dispatch(
        db_session, test_engine, tmp_path, printer_factory, monkeypatch
    )
    start = MagicMock(return_value=False)
    monkeypatch.setattr(bd.printer_manager, "start_print", start)

    for _ in range(DISPATCH_MAX_ATTEMPTS + 1):
        await one_tick(scheduler, spawned)
        row = await reread(db_session, PrintQueueItem, item_id)
        assert row.status == "pending" and row.dispatch_attempts == 0

    assert start.call_count == DISPATCH_MAX_ATTEMPTS + 1


async def test_attempts_a_crash_spent_end_in_a_visible_failure_and_a_retry_opens_a_new_budget(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions
):
    import backend.app.services.background_dispatch as bd
    from backend.app.api.routes.print_queue import retry_failed_item
    from backend.app.services.print_scheduler import DISPATCH_MAX_ATTEMPTS

    queue_id, _mqtt, item_id, _service, scheduler, spawned = await a_job_and_its_dispatch(
        db_session, test_engine, tmp_path, printer_factory, monkeypatch
    )
    claimed_before = datetime(2026, 10, 1, 12, 0)
    row = await db_session.get(PrintQueueItem, item_id)
    row.dispatch_attempts = DISPATCH_MAX_ATTEMPTS  # three crashes mid-dispatch spent them
    row.started_at = claimed_before
    await db_session.commit()
    start = MagicMock(return_value=True)
    monkeypatch.setattr(bd.printer_manager, "start_print", start)

    await one_tick(scheduler, spawned)

    start.assert_not_called()
    row = await reread(db_session, PrintQueueItem, item_id)
    assert row.status == "failed" and row.error_code == "dispatchInternalError"
    assert "budget" in row.error_message
    assert (await db_session.get(PrinterQueue, queue_id)).status == "error"

    await retry_failed_item(item_id, db=db_session, _=None)
    row = await reread(db_session, PrintQueueItem, item_id)
    assert row.status == "pending" and row.dispatch_attempts == 0
    assert row.error_code is None and row.error_message is None
    # The proof of an earlier dispatch survives the Retry (swap_job_refresh reads it).
    assert row.started_at == claimed_before
    # A Retry is not a Resume: the halted queue stays halted.
    assert (await db_session.get(PrinterQueue, queue_id)).status == "error"


# --------------------------------------------------------------------------- #
# D09 — a refusal that ends the job and halts its queue tells the operator, once
# --------------------------------------------------------------------------- #


@pytest.fixture
def notice(monkeypatch):
    from backend.app.services.notification_service import notification_service

    sent = AsyncMock()
    monkeypatch.setattr(notification_service, "on_queue_job_failed", sent)
    return sent


@pytest.fixture
def power_off(monkeypatch):
    from backend.app.services.print_scheduler import PrintScheduler

    off = AsyncMock()
    monkeypatch.setattr(PrintScheduler, "_power_off_if_needed", off)
    return off


def the_reason(notice) -> str:
    return notice.await_args.kwargs["reason"]


async def test_an_exhausted_budget_tells_the_operator_once_with_its_class(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions, notice, power_off
):
    from backend.app.services.print_scheduler import DISPATCH_MAX_ATTEMPTS
    from backend.app.utils import failure_reasons

    _queue_id, mqtt, item_id, _service, scheduler, spawned = await a_job_and_its_dispatch(
        db_session, test_engine, tmp_path, printer_factory, monkeypatch
    )
    row = await db_session.get(PrintQueueItem, item_id)
    row.dispatch_attempts = DISPATCH_MAX_ATTEMPTS
    await db_session.commit()

    await one_tick(scheduler, spawned)
    await one_tick(scheduler, spawned)

    mqtt._client.publish.assert_not_called()
    row = await reread(db_session, PrintQueueItem, item_id)
    assert row.status == "failed"
    notice.assert_awaited_once()
    assert the_reason(notice) == failure_reasons.dispatch_reason_text(row.error_code, row.error_message)
    power_off.assert_awaited_once()  # the policy of every refusal before a claim


async def test_an_exhausted_budget_that_did_not_commit_tells_nobody(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions, notice
):
    import backend.app.services.queue_counters as counters
    from backend.app.services.print_scheduler import DISPATCH_MAX_ATTEMPTS

    _queue_id, _mqtt, item_id, _service, scheduler, spawned = await a_job_and_its_dispatch(
        db_session, test_engine, tmp_path, printer_factory, monkeypatch
    )
    row = await db_session.get(PrintQueueItem, item_id)
    row.dispatch_attempts = DISPATCH_MAX_ATTEMPTS
    await db_session.commit()
    monkeypatch.setattr(counters, "halt_lane", AsyncMock(side_effect=RuntimeError("database is locked")))

    with pytest.raises(RuntimeError):
        await scheduler.check_queue()

    notice.assert_not_awaited()
    assert (await reread(db_session, PrintQueueItem, item_id)).status == "pending"


@pytest.mark.parametrize("refusal", ["swap_start_ran", "source_failed"])
async def test_a_refusal_that_ends_the_job_tells_the_operator_once_with_its_class(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions, notice, power_off, refusal
):
    import backend.app.services.background_dispatch as bd
    from backend.app.services.filament_deferred import DISPOSITION_FAILED, SourceCaptureDeferred
    from backend.app.services.filament_routing import RoutingDeferred
    from backend.app.utils import failure_reasons

    queue_id, mqtt, item_id, service, scheduler, spawned = await a_job_and_its_dispatch(
        db_session, test_engine, tmp_path, printer_factory, monkeypatch
    )
    if refusal == "swap_start_ran":

        async def the_start_macro_ran(job, printer, event, message, *, record=None):
            if event == "swap_mode_start":
                job.swap_start_effect = "fired"
                if record is not None:
                    await record("fired")

        monkeypatch.setattr(service, "_run_swap_macro_if_needed", the_start_macro_ran)
        monkeypatch.setattr(bd, "settle_plan", AsyncMock(side_effect=RoutingDeferred("feed_settle_timeout")))
    else:
        failed = SourceCaptureDeferred("source_timeout", disposition=DISPOSITION_FAILED)
        monkeypatch.setattr(bd, "settle_plan", AsyncMock(side_effect=failed))

    await one_tick(scheduler, spawned)
    await one_tick(scheduler, spawned)

    mqtt._client.publish.assert_not_called()
    row = await reread(db_session, PrintQueueItem, item_id)
    assert row.status == "failed"
    assert (await db_session.get(PrinterQueue, queue_id)).status == "error"
    notice.assert_awaited_once()
    assert the_reason(notice) == failure_reasons.dispatch_reason_text(row.error_code, row.error_message)
    if refusal == "swap_start_ran":
        # The table may have moved: the printer is left as it is for the operator (review r1).
        power_off.assert_not_awaited()
    else:
        power_off.assert_awaited_once()  # nothing was published: the policy of a refusal before the start


async def test_a_refusal_after_a_sent_start_is_told_but_adds_no_physical_action(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions, notice, power_off
):
    """A refusal that arrives with the start already sent ends the row and is reported, but
    the printer's state is unknown: no auto-off is scheduled on top of it."""
    import backend.app.services.background_dispatch as bd

    _queue_id, _mqtt, item_id, _service, scheduler, spawned = await a_job_and_its_dispatch(
        db_session, test_engine, tmp_path, printer_factory, monkeypatch
    )
    real_run = bd.background_dispatch.run_from_queue_item

    async def a_refusal_after_the_send(*args, **kwargs):
        outcome = await real_run(*args, **kwargs)
        return {
            **outcome,
            "success": False,
            "deferred": True,
            "disposition": None,
            "reason": {"code": "feed_settle_timeout", "message": "The feed did not settle."},
            "send_fact": "sent",
            "failure_code": "dispatchStartRefused",
        }

    monkeypatch.setattr(bd.background_dispatch, "run_from_queue_item", a_refusal_after_the_send)

    await one_tick(scheduler, spawned)

    assert (await reread(db_session, PrintQueueItem, item_id)).status == "failed"
    notice.assert_awaited_once()
    power_off.assert_not_awaited()


async def test_a_refusal_that_only_waits_tells_nobody(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions, notice, power_off
):
    import backend.app.services.background_dispatch as bd
    from backend.app.services.filament_routing import RoutingDeferred

    _queue_id, _mqtt, item_id, _service, scheduler, spawned = await a_job_and_its_dispatch(
        db_session, test_engine, tmp_path, printer_factory, monkeypatch
    )
    monkeypatch.setattr(bd, "settle_plan", AsyncMock(side_effect=RoutingDeferred("feed_settle_timeout")))

    await one_tick(scheduler, spawned)

    assert (await reread(db_session, PrintQueueItem, item_id)).status == "pending"
    notice.assert_not_awaited()
    power_off.assert_not_awaited()


# --------------------------------------------------------------------------- #
# D06 — a shutdown is not a failed print
# --------------------------------------------------------------------------- #


async def test_a_shutdown_or_an_unknown_outcome_announces_no_failure(monkeypatch):
    import backend.app.services.background_dispatch as bd

    notify = AsyncMock()
    monkeypatch.setattr("backend.app.services.notification_service.notification_service.on_queue_job_failed", notify)
    job = bd.PrintDispatchJob(
        id=1, kind="print_library_file", source_id=1, source_name="lamp", printer_id=1, printer_name="P"
    )
    job.send_fact = bd.SEND_SENT
    job.interrupted = True
    await bd.report_failure_if_unwatched(job)
    notify.assert_not_awaited()

    job.interrupted = False
    job.outcome["uncertain"] = True
    await bd.report_failure_if_unwatched(job)
    notify.assert_not_awaited()


@pytest.mark.parametrize("verdict", ["accepted", "observed_in_start_callback", "not_started"])
async def test_dispatch_color_changes_follow_a_proven_start(db_session, monkeypatch, verdict):
    from backend.app.services import dispatch_phase
    from backend.app.services.background_dispatch import BackgroundDispatchService, DispatchNotStarted
    from backend.app.services.filament_preflight import DispatchRoutingGuard
    from backend.app.services.filament_routing import RoutingPolicy
    from backend.app.services.printer_feed_snapshot import FeedSource

    archive = PrintArchive(
        filename="mapped.3mf",
        file_path="",
        file_size=1,
        status="printing",
        dispatch_phase=dispatch_phase.PREPARED,
        dispatch_boot_id="color-test",
        extra_data={"dispatch_intent": {"version": 1, "remote_filename": "mapped.3mf"}},
    )
    db_session.add(archive)
    await db_session.commit()
    job = SimpleNamespace(
        options={},
        execution_archive_id=archive.id,
        dispatch_boot_id="color-test",
        printer_id=1,
        routing_guard=DispatchRoutingGuard(
            policy=RoutingPolicy(),
            exact_model=True,
            revision="color-test",
            generation=1,
            nozzle_diameters={},
            left_tpu_check=False,
            requirements=SimpleNamespace(
                used_filaments=(
                    {"slot_id": 3, "color": "#000000"},
                    {"slot_id": 4, "color": "#FFFFFF"},
                )
            ),
            plan=SimpleNamespace(
                assignments={
                    3: FeedSource(0, "ams", "PLA", "FF0000FF", declared_color="000000FF"),
                    4: FeedSource(1, "ams", "PLA", "#FFFFFF"),
                }
            ),
        ),
    )
    service = BackgroundDispatchService()
    assert await service._send_start(db_session, job, lambda **_: True)
    await db_session.refresh(archive)
    assert "dispatch_color_changes" not in archive.extra_data
    if verdict == "observed_in_start_callback":
        assert dispatch_phase.mark_seen_running(archive)
        await db_session.commit()
    else:
        answer = "accepted" if verdict == "accepted" else "no_acknowledgement"
        monkeypatch.setattr(service, "_await_print_response", AsyncMock(return_value=answer))
        if verdict == "not_started":
            with pytest.raises(DispatchNotStarted):
                await service._require_running(db_session, job, "Printer", "IDLE")
        else:
            await service._require_running(db_session, job, "Printer", "IDLE")
    await db_session.refresh(archive)
    assert archive.extra_data["dispatch_intent"]["remote_filename"] == "mapped.3mf"
    if verdict == "not_started":
        assert "dispatch_color_changes" not in archive.extra_data
    else:
        assert archive.extra_data["dispatch_color_changes"] == [
            {"slot_id": 3, "requested_color": "#000000", "loaded_color": "#FF0000"}
        ]


@pytest.mark.parametrize(
    ("planned", "live", "verdict"),
    [
        ("00FF00FF", "FF0000FF", "deferred"),
        ("00FF00FF", "000000FF", "deferred"),
        ("000000FF", "FF0000FF", "deferred"),
        ("FF0000FF", "FF0000", "accepted"),
        ("FF0000FF", "FF0000FF", "observed_in_start_callback"),
        ("FF0000FF", "FF0000FF", "not_started"),
    ],
)
async def test_color_evidence_is_checked_under_the_mqtt_publish_lock(db_session, monkeypatch, planned, live, verdict):
    from dataclasses import replace
    from functools import partial

    from backend.app.services import dispatch_phase
    from backend.app.services.background_dispatch import BackgroundDispatchService, DispatchNotStarted
    from backend.app.services.bambu_mqtt import BambuMQTTClient
    from backend.app.services.farm_startup_guard import farm_startup_guard
    from backend.app.services.filament_preflight import DispatchRoutingGuard
    from backend.app.services.filament_requirements import PrintRequirements
    from backend.app.services.filament_routing import RoutingDeferred, RoutingPlan, RoutingPolicy
    from backend.app.services.printer_feed_snapshot import FeedSource, PrinterFeedSnapshot

    archive = PrintArchive(
        filename="mapped.3mf",
        file_path="",
        file_size=1,
        status="printing",
        dispatch_phase=dispatch_phase.PREPARED,
        dispatch_boot_id="mqtt-color-test",
        extra_data={"dispatch_intent": {"version": 1, "remote_filename": "mapped.3mf"}},
    )
    db_session.add(archive)
    await db_session.commit()
    former = FeedSource(0, "ams", "PLA", color=planned, nozzles=(0,), identity="former")
    replacement = replace(former, color=live, identity="replacement")
    requirements = PrintRequirements(
        "ok",
        resolved_plate_id=1,
        model="X1C",
        used_filaments=({"slot_id": 1, "type": "PLA", "color": "#000000", "used_grams": 1},),
    )
    policy = RoutingPolicy()
    plan = RoutingPlan(1, 1, {}, policy.fingerprint, (1, "before"), {1: former}, 0)
    guard = DispatchRoutingGuard(requirements, policy, plan, True, "before", 1, {}, False)
    before = PrinterFeedSnapshot(1, "X1C", True, 1, "before", True, True, True, (former,))
    at_publish = replace(before, revision="after", sources=(replacement,))
    guard.validate(before, mapping=plan.mapping, use_ams=plan.use_ams, plate_id=1)

    mqtt = BambuMQTTClient("127.0.0.1", "G03-C-COLOR", "00000000", model="X1C")
    mqtt.state.connected = True
    mqtt.state.state = "IDLE"
    mqtt._client = MagicMock()
    monkeypatch.setattr(mqtt, "get_feed_snapshot", lambda _: at_publish)
    monkeypatch.setattr(farm_startup_guard, "require", lambda: None)
    service = BackgroundDispatchService()
    job = SimpleNamespace(
        options={},
        execution_archive_id=archive.id,
        dispatch_boot_id="mqtt-color-test",
        printer_id=1,
        routing_guard=guard,
    )
    start = partial(
        mqtt.start_print,
        "mapped.3mf",
        ams_mapping=plan.mapping,
        use_ams=plan.use_ams,
        routing_guard=guard,
    )
    if verdict == "deferred":
        with pytest.raises(RoutingDeferred, match="feed_state_changed"):
            await service._send_start(db_session, job, start)
        mqtt._client.publish.assert_not_called()
        assert job.send_fact == "refused"
        await db_session.refresh(archive)
        assert "dispatch_color_changes" not in archive.extra_data
        return

    assert await service._send_start(db_session, job, start)
    mqtt._client.publish.assert_called_once()
    await db_session.refresh(archive)
    assert "dispatch_color_changes" not in archive.extra_data
    if verdict == "observed_in_start_callback":
        assert dispatch_phase.mark_seen_running(archive)
        await db_session.commit()
    else:
        answer = "accepted" if verdict == "accepted" else "no_acknowledgement"
        monkeypatch.setattr(service, "_await_print_response", AsyncMock(return_value=answer))
        if verdict == "not_started":
            with pytest.raises(DispatchNotStarted):
                await service._require_running(db_session, job, "Printer", "IDLE")
        else:
            await service._require_running(db_session, job, "Printer", "IDLE")
    await db_session.refresh(archive)
    if verdict == "not_started":
        assert "dispatch_color_changes" not in archive.extra_data
    else:
        assert archive.extra_data["dispatch_color_changes"] == [
            {"slot_id": 1, "requested_color": "#000000", "loaded_color": "#FF0000"}
        ]
