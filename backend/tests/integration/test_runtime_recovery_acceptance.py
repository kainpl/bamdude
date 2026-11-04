"""End to end: a finished run held in memory, the loop it causes, its repair and the next print (A17, A25).

Spec: vault ``60-specs/printer-runtime-stall-diagnostics-spec.md`` (r3). Every step is a production path:
the dispatch's own binding and its empty macro selection, the connect-edge sweep that closes the run in the
database, the real scheduler and runners (device I/O faked), the addressed preview and its two commits, and
the queue's own Resume route. Printer observations are synthetic; no physical command is expected anywhere
before the operator resumes the queue.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.print_state_recovery_action import PrintStateRecoveryAction
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import (
    dispatch_stall_tracker as tracker,
    preheat,
    print_state_recovery_preview as tokens,
    print_state_recovery_store as store,
)
from backend.app.services.print_run_binding import bind_prepared_print_run, current_print_run
from backend.app.services.print_state_recovery import RUNTIME_ACTION, classify
from backend.app.services.printer_manager import printer_manager
from backend.tests.integration.test_dispatch_attempt_verdicts import a_job_and_its_dispatch, one_tick
from backend.tests.integration.test_dispatch_without_original import (  # noqa: F401 - fixtures
    clean_spool_state,
    sessions,
)
from backend.tests.integration.test_printer_runtime_stall import (  # noqa: F401 - fixtures
    context_arrives_during_upload,
    runtime,
    stall_clock,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def the_printer_reports(mqtt, state: str) -> None:
    """A status push through the client's own message handler, which stamps when it was received."""
    payload = {"print": {"command": "push_status", "gcode_state": state}}
    mqtt._on_message(None, None, SimpleNamespace(topic="device/SYNTHETIC/report", payload=json.dumps(payload).encode()))


async def a_finished_run_held_over_a_pending_job(
    db, test_engine, tmp_path, printer_factory, archive_factory, monkeypatch
):
    """A01 on a printer whose queue holds a pending job: the closer ends A in the database, A stays bound in memory.

    The closer alone (``_reconcile``), not the connect-edge wrapper: since gh109-o3 r2 the wrapper releases the
    exact run its own close ended, after its outer commit. What stays bound here is a run whose release did not
    happen -- the operator's audited repair below is the path for that (``test_reconcile_retires_its_own_run.py``
    holds the wrapper's own release and every case it leaves to this repair).
    """
    import backend.app.services.print_scheduler as ps
    from backend.app import main
    from backend.app.api.routes.printers import clear_plate
    from backend.app.services.print_reconciliation import _reconcile

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(main, "async_session", factory)
    # The real registration, taken before the dispatch harness replaces it with a recorder.
    register = main.register_macro_selection
    queue_id, mqtt, pending_id, service, scheduler, spawned = await a_job_and_its_dispatch(
        db, test_engine, tmp_path, printer_factory, monkeypatch
    )
    # The store asks the scheduler that dispatches whether a dispatch is in flight.
    monkeypatch.setattr(ps, "scheduler", scheduler)
    queue = await db.get(PrinterQueue, queue_id)
    printer_id = queue.printer_id
    started = datetime.now(timezone.utc) - timedelta(hours=2)
    archive = await archive_factory(
        printer_id,
        queue_id=queue_id,
        filename="cube.gcode.3mf",
        status="printing",
        started_at=started,
        completed_at=None,
        created_at=started,
        extra_data={},
    )
    item = PrintQueueItem(queue_id=queue_id, archive_id=archive.id, status="printing", started_at=started, position=0)
    db.add(item)
    await db.flush()
    queue.status = "printing"
    queue.current_item_id = item.id
    await db.commit()
    run = bind_prepared_print_run(
        printer_manager,
        printer_id=printer_id,
        archive_id=archive.id,
        queue_item_id=item.id,
        claim_started_at=started,
        expected_submission_id="111",
    )
    assert run is not None
    register(printer_id, {"selected_macro_ids": []})

    mqtt.state.state = "FINISH"
    assert await _reconcile(db, printer_id, "FINISH", "cube.gcode.3mf") == [archive.id]
    await db.commit()
    assert current_print_run(printer_manager, printer_id) is run
    # Plate clear removes that gate; the reconciled run still vetoes a new attempt.
    await clear_plate(printer_id, None, current_user=None, db=db)
    assert not printer_manager.is_awaiting_plate_clear(printer_id)
    the_printer_reports(mqtt, "FINISH")
    return SimpleNamespace(
        printer_id=printer_id,
        queue_id=queue_id,
        archive_id=archive.id,
        pending_id=pending_id,
        mqtt=mqtt,
        service=service,
        scheduler=scheduler,
        spawned=spawned,
        factory=factory,
        run=run,
    )


async def preview(db, a):
    response = await store.make_preview(db, a.printer_id, "runtime", a.archive_id, RUNTIME_ACTION, "user:1")
    return response, tokens.verify(response["preview_token"])


async def reread(db, model, row_id):
    db.expire_all()
    return await db.get(model, row_id)


async def attempts(db, a) -> int:
    db.expire_all()
    return await db.scalar(
        select(func.count()).select_from(PrintArchive).where(PrintArchive.printer_id == a.printer_id)
    )


async def audit_rows(db) -> int:
    return await db.scalar(select(func.count()).select_from(PrintStateRecoveryAction))


async def test_a_released_context_waits_under_the_pause_and_resume_allows_one_attempt(
    db_session, test_engine, tmp_path, printer_factory, archive_factory, monkeypatch, sessions, runtime, stall_clock
):
    """A17: deferred loop → repair between attempts → pending waits under the pause → Resume → one start."""
    from backend.app import main
    from backend.app.api.routes.printer_queues import update_queue
    from backend.app.schemas.printer_queue import PrinterQueueUpdate

    a = await a_finished_run_held_over_a_pending_job(
        db_session, test_engine, tmp_path, printer_factory, archive_factory, monkeypatch
    )
    held = await reread(db_session, PrintArchive, a.archive_id)
    snapshot = (held.status, held.completed_at, dict(held.extra_data or {}))

    for _ in range(2):
        await one_tick(a.scheduler, a.spawned)
        stall_clock.advance(40)
    assert (await reread(db_session, PrintQueueItem, a.pending_id)).status == "pending"
    assert tracker.snapshot(a.printer_id) == ()  # early waiting is not a dispatch attempt
    a.mqtt._client.publish.assert_not_called()

    # The early wait has no episode, but the runtime context itself still has its repair.
    response, payload = await preview(db_session, a)
    assert response["after"]["runtime"]["macro_selection"] is True
    queue_before = await reread(db_session, PrinterQueue, a.queue_id)
    status_before = queue_before.status
    result = await store.apply_preview(a.factory, payload, "user:1", "Operator")
    assert result["outcome"] == "applied", result

    assert current_print_run(printer_manager, a.printer_id) is None
    assert a.printer_id not in main._active_macro_selection
    held = await reread(db_session, PrintArchive, a.archive_id)
    assert (held.status, held.completed_at, dict(held.extra_data or {})) == snapshot
    queue = await reread(db_session, PrinterQueue, a.queue_id)
    assert queue.is_paused is True and queue.status == status_before
    a.mqtt._client.publish.assert_not_called()

    # The scheduler does not start the pending job under the pause.
    before = await attempts(db_session, a)
    await one_tick(a.scheduler, a.spawned)
    pending = await reread(db_session, PrintQueueItem, a.pending_id)
    assert pending.status == "pending" and pending.started_at is None
    assert await attempts(db_session, a) == before
    a.mqtt._client.publish.assert_not_called()

    # The operator's own Resume, through the queue's route, allows one ordinary start.
    await update_queue(a.queue_id, PrinterQueueUpdate(is_paused=False), db=db_session, _=None)
    await one_tick(a.scheduler, a.spawned)

    a.mqtt._client.publish.assert_called_once()
    started = await reread(db_session, PrintQueueItem, a.pending_id)
    assert started.status == "printing", started.error_message
    execution = await db_session.get(PrintArchive, started.archive_id)
    assert execution.status == "printing"
    assert current_print_run(printer_manager, a.printer_id).archive_id == started.archive_id
    # The successful bind ended the episode.
    assert not [e for e in tracker.snapshot(a.printer_id) if e.stalled]


@pytest.mark.parametrize("stage", ["upload", "preheat"])
async def test_a_repair_asked_while_an_attempt_runs_is_refused(
    db_session,
    test_engine,
    tmp_path,
    printer_factory,
    archive_factory,
    monkeypatch,
    sessions,
    runtime,
    stall_clock,
    stage,
):
    """A17: during an upload or a live preheat the attempt owns the printer -- the repair is refused, never
    run around the in-flight work; once the attempt has ended the same repair is offered."""
    import backend.app.services.background_dispatch as bd

    a = await a_finished_run_held_over_a_pending_job(
        db_session, test_engine, tmp_path, printer_factory, archive_factory, monkeypatch
    )
    answers, reasons = [], []
    key = (a.printer_id, "runtime", a.archive_id)

    async def ask_for_a_repair(*_args, **_kwargs):
        if current_print_run(printer_manager, a.printer_id) is None:
            printer_manager._print_run_bindings[a.printer_id] = a.run
        async with a.factory() as db:
            try:
                await store.make_preview(db, a.printer_id, "runtime", a.archive_id, RUNTIME_ACTION, "user:1")
            except ValueError as refusal:
                answers.append(str(refusal))
            else:
                answers.append("offered")
            facts, _, _ = await store.read_facts(db, [a.printer_id], [key])
            reasons.extend(classify(facts[key])["blocked_reasons"])
        return True

    context_arrives_during_upload(monkeypatch, a.printer_id)
    if stage == "upload":
        monkeypatch.setattr(bd, "upload_file_async", ask_for_a_repair)
    else:
        monkeypatch.setattr("backend.app.services.preheat.preheat_and_soak", ask_for_a_repair)

    await one_tick(a.scheduler, a.spawned)

    assert answers == ["recovery_not_allowed"], answers
    # The attempt itself is the owner the repair waits for -- not only the queue row it claimed.
    assert "context_owner_active" in reasons and "queue_busy" in reasons, reasons
    assert current_print_run(printer_manager, a.printer_id) is a.run
    assert await audit_rows(db_session) == 0
    assert (await reread(db_session, PrinterQueue, a.queue_id)).is_paused is False
    assert (await reread(db_session, PrintQueueItem, a.pending_id)).status == "pending"
    a.mqtt._client.publish.assert_not_called()

    # The control: with nothing in flight the same repair is offered.
    response, _payload = await preview(db_session, a)
    assert response["action"] == RUNTIME_ACTION


async def test_a_swap_still_owed_keeps_everything_through_the_loop(
    db_session, test_engine, tmp_path, printer_factory, archive_factory, monkeypatch, sessions, runtime, stall_clock
):
    """A17 negative control: a pending swap obligation of A has no repair; the loop and A stay as they are."""
    from backend.app import main

    a = await a_finished_run_held_over_a_pending_job(
        db_session, test_engine, tmp_path, printer_factory, archive_factory, monkeypatch
    )
    held = await reread(db_session, PrintArchive, a.archive_id)
    held.extra_data = {**(held.extra_data or {}), "bamdude_terminal_effects": {"swap_change_table": {"stage": "owed"}}}
    await db_session.commit()

    await one_tick(a.scheduler, a.spawned)
    with pytest.raises(ValueError, match="recovery_not_allowed"):
        await preview(db_session, a)
    await one_tick(a.scheduler, a.spawned)

    assert current_print_run(printer_manager, a.printer_id) is a.run
    assert a.printer_id in main._active_macro_selection
    assert await audit_rows(db_session) == 0
    assert (await reread(db_session, PrinterQueue, a.queue_id)).is_paused is False
    held = await reread(db_session, PrintArchive, a.archive_id)
    assert held.extra_data["bamdude_terminal_effects"] == {"swap_change_table": {"stage": "owed"}}
    assert (await reread(db_session, PrintQueueItem, a.pending_id)).status == "pending"
    a.mqtt._client.publish.assert_not_called()


async def test_an_open_timelapse_keeps_the_next_print_out_of_it(
    db_session, test_engine, tmp_path, printer_factory, archive_factory, monkeypatch, sessions, runtime, stall_clock
):
    """RS-R2 end to end: A's layer timelapse is still open and the next job's printer has no external camera,
    so nothing would replace the session. The repair is refused with that reason; the job stays deferred and
    not one of its layers reaches A's session -- no B ever starts on this printer while A is held."""
    from backend.app.services import layer_timelapse

    a = await a_finished_run_held_over_a_pending_job(
        db_session, test_engine, tmp_path, printer_factory, archive_factory, monkeypatch
    )
    capture = AsyncMock()
    monkeypatch.setattr(
        layer_timelapse,
        "_active_sessions",
        {a.printer_id: SimpleNamespace(archive_id=a.archive_id, capture_layer=capture)},
    )
    start_session = MagicMock(wraps=layer_timelapse.start_session)
    monkeypatch.setattr(layer_timelapse, "start_session", start_session)

    for _ in range(2):
        await one_tick(a.scheduler, a.spawned)
        stall_clock.advance(40)
    with pytest.raises(ValueError, match="recovery_not_allowed"):
        await preview(db_session, a)
    await one_tick(a.scheduler, a.spawned)

    assert current_print_run(printer_manager, a.printer_id) is a.run
    assert (await reread(db_session, PrintQueueItem, a.pending_id)).status == "pending"
    start_session.assert_not_called()  # no external camera: nothing would have replaced A's session
    capture.assert_not_awaited()
    a.mqtt._client.publish.assert_not_called()
    assert await audit_rows(db_session) == 0


async def test_a_residual_preheat_pin_is_shown_kept_and_never_blocks(
    db_session, test_engine, tmp_path, printer_factory, archive_factory, monkeypatch, sessions, runtime, stall_clock
):
    """A25: what an earlier attempt's preheat left pinned is named, survives the repair untouched, and the
    repair sends nothing to the heaters."""
    a = await a_finished_run_held_over_a_pending_job(
        db_session, test_engine, tmp_path, printer_factory, archive_factory, monkeypatch
    )
    await one_tick(a.scheduler, a.spawned)
    # What a real preheat records before the bind refuses the attempt (the harness stubs the preheat itself).
    monkeypatch.setitem(preheat._pinned, a.printer_id, {"bed", "chamber"})

    response, payload = await preview(db_session, a)
    assert response["action"] == RUNTIME_ACTION
    assert response["runtime"]["preheat_pinned"] == ["bed", "chamber"]
    result = await store.apply_preview(a.factory, payload, "user:1", "Operator")

    assert result["outcome"] == "applied", result
    assert preheat.pinned(a.printer_id) == ["bed", "chamber"]
    assert current_print_run(printer_manager, a.printer_id) is None
    a.mqtt._client.publish.assert_not_called()
