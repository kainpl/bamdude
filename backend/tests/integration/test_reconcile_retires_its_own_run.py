"""The sweep that closes the exact run it found still bound releases that run's context (spec gh109-o3 r2, D2 = C).

A print that finished while the printer's MQTT session was down is closed by the connect-edge sweep; the dispatch's
binding of that run stayed in memory, and since GH109 it vetoed every next attempt until an operator repaired it.
After a restart the binding does not exist and the same close goes straight on to the next job, so the sweep that
itself took the run from printing to completed/failed now ends that run's context too -- once, after its outer
commit, with one synchronous compare-and-clear against what it captured, and without any new pause.

Everything else stays the operator's audited repair: uncertain and checkpoint closes, a swap still owed, a changed
context, another printer life. Device I/O never happens here; printer states are synthetic.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import print_reconciliation, print_run_binding as prb
from backend.app.services.printer_manager import printer_manager
from backend.tests.integration.test_dispatch_without_original import clean_spool_state, sessions  # noqa: F401
from backend.tests.integration.test_printer_runtime_stall import runtime, stall_clock  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

FILE = "cube.gcode.3mf"


@pytest.fixture
def sweep_db(monkeypatch, test_engine):
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("backend.app.core.database.async_session", factory)
    monkeypatch.setattr("backend.app.main.async_session", factory)
    monkeypatch.setattr("backend.app.core.tasks.spawn_background_task", lambda coro, **_k: coro.close())
    return factory


async def a_bound_printing_run(db, printer_factory, archive_factory, *, extra=None, paused=False, bind=True):
    """A dispatched run still printing in the database and still bound in memory -- what a missed finish leaves."""
    printer_id = (await printer_factory(ip_address="192.0.2.20")).id
    started = datetime.now(timezone.utc) - timedelta(hours=2)
    queue = PrinterQueue(id=printer_id, printer_id=printer_id, status="printing", is_paused=paused)
    db.add(queue)
    await db.flush()
    archive = await archive_factory(
        printer_id,
        queue_id=printer_id,
        filename=FILE,
        file_path="",
        status="printing",
        started_at=started,
        completed_at=None,
        created_at=started,
        extra_data=dict(extra or {}),
    )
    item = PrintQueueItem(queue_id=printer_id, archive_id=archive.id, status="printing", started_at=started, position=0)
    db.add(item)
    await db.flush()
    queue.current_item_id = item.id
    await db.commit()
    run = None
    if bind:
        run = prb.bind_prepared_print_run(
            printer_manager,
            printer_id=printer_id,
            archive_id=archive.id,
            queue_item_id=item.id,
            claim_started_at=started,
            expected_submission_id="111",
        )
        assert run is not None
    return SimpleNamespace(printer_id=printer_id, archive_id=archive.id, item_id=item.id, run=run, started=started)


async def sweep(a, state="FINISH", file=FILE):
    await print_reconciliation.reconcile_printer_prints(a.printer_id, state, file)


async def reread(db, model, row_id):
    db.expire_all()
    return await db.get(model, row_id)


# --------------------------------------------------------------------------- #
# A01 / A13 -- its own completed or failed close releases the run, with no new pause
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("state", "status", "queue_status"), [("FINISH", "completed", "idle"), ("FAILED", "failed", "error")]
)
@pytest.mark.parametrize("paused", [False, True], ids=["running-queue", "operator-paused-queue"])
async def test_the_sweeps_own_close_releases_the_run_and_adds_no_pause(
    db_session, printer_factory, archive_factory, sweep_db, state, status, queue_status, paused
):
    a = await a_bound_printing_run(db_session, printer_factory, archive_factory, paused=paused)

    await sweep(a, state)

    assert (await reread(db_session, PrintArchive, a.archive_id)).status == status
    assert prb.current_print_run(printer_manager, a.printer_id) is None
    assert prb.terminal_accepted(printer_manager, a.printer_id, a.run.effect_generation)  # a late terminal is a dup
    assert prb.effect_generation(printer_manager, a.printer_id) == a.run.effect_generation  # not advanced
    queue = await reread(db_session, PrinterQueue, a.printer_id)
    assert queue.status == queue_status
    assert bool(queue.is_paused) is paused  # neither added nor cleared
    if status == "completed":
        assert printer_manager.is_awaiting_plate_clear(a.printer_id)  # the close's own gate stands


async def test_without_a_binding_the_close_is_what_it_always_was(
    db_session, printer_factory, archive_factory, sweep_db
):
    """A13: after a restart there is no binding -- the close idles the queue as before, nothing is paused."""
    a = await a_bound_printing_run(db_session, printer_factory, archive_factory, bind=False)

    await sweep(a)

    queue = await reread(db_session, PrinterQueue, a.printer_id)
    assert queue.status == "idle" and not queue.is_paused
    assert not prb.has_runtime_context(printer_manager, a.printer_id)


# --------------------------------------------------------------------------- #
# A02 / A06 -- nothing the sweep did not itself close cleanly is released
# --------------------------------------------------------------------------- #


async def test_an_archive_already_terminal_on_entry_is_not_released(
    db_session, printer_factory, archive_factory, sweep_db
):
    a = await a_bound_printing_run(db_session, printer_factory, archive_factory)
    archive = await db_session.get(PrintArchive, a.archive_id)
    archive.status = "completed"
    await db_session.commit()

    await sweep(a)

    assert prb.current_print_run(printer_manager, a.printer_id) is a.run


async def test_a_binding_of_another_archive_is_not_released(db_session, printer_factory, archive_factory, sweep_db):
    a = await a_bound_printing_run(db_session, printer_factory, archive_factory, bind=False)
    other = prb.bind_print_run(
        printer_manager, printer_id=a.printer_id, archive_id=a.archive_id + 1000, origin="dispatch"
    )

    await sweep(a)

    assert (await reread(db_session, PrintArchive, a.archive_id)).status == "completed"
    assert prb.current_print_run(printer_manager, a.printer_id) is other


async def test_the_same_archive_id_under_another_claim_is_not_released(
    db_session, printer_factory, archive_factory, sweep_db
):
    """A numeric id is no proof: the binding names another queue claim than the row the sweep closed."""
    a = await a_bound_printing_run(db_session, printer_factory, archive_factory, bind=False)
    stranger = prb.bind_prepared_print_run(
        printer_manager,
        printer_id=a.printer_id,
        archive_id=a.archive_id,
        queue_item_id=a.item_id + 500,
        claim_started_at=a.started,
        expected_submission_id="222",
    )

    await sweep(a)

    assert prb.current_print_run(printer_manager, a.printer_id) is stranger


@pytest.mark.parametrize("kind", ["uncertain", "checkpoint", "swap_owed"])
async def test_a_close_that_proves_less_keeps_the_context(db_session, printer_factory, archive_factory, sweep_db, kind):
    extra = None
    if kind == "swap_owed":
        extra = {"swap_macro_events_pending": ["swap_mode_change_table"]}
    a = await a_bound_printing_run(db_session, printer_factory, archive_factory, extra=extra)
    if kind == "checkpoint":
        from backend.app.main import _TERMINAL_ACCEPTANCE_KEY

        archive = await db_session.get(PrintArchive, a.archive_id)
        archive.extra_data = {
            _TERMINAL_ACCEPTANCE_KEY: {
                "version": 2,
                "stage": "accepted",
                "outcome": "completed",
                "queue_item_id": a.item_id,
                "queue_started_at": a.started.isoformat(),
            }
        }
        await db_session.commit()

    await sweep(a, file="something-else.gcode.3mf" if kind == "uncertain" else FILE)

    assert (await reread(db_session, PrintArchive, a.archive_id)).status != "printing"  # the sweep did close it
    assert prb.current_print_run(printer_manager, a.printer_id) is a.run


# --------------------------------------------------------------------------- #
# A03 -- no confirmed outer commit, no release
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("failure", [RuntimeError("outer commit refused"), asyncio.CancelledError()])
async def test_an_outer_commit_that_did_not_complete_keeps_the_context(
    db_session, printer_factory, archive_factory, sweep_db, monkeypatch, failure
):
    a = await a_bound_printing_run(db_session, printer_factory, archive_factory)
    real = print_reconciliation._reconcile

    async def closed_then_the_outer_commit_fails(db, *args, **kwargs):
        result = await real(db, *args, **kwargs)  # the archive's own commit is inside

        async def refuse():
            raise failure

        db.commit = refuse
        return result

    monkeypatch.setattr(print_reconciliation, "_reconcile", closed_then_the_outer_commit_fails)
    if isinstance(failure, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await sweep(a)
    else:
        await sweep(a)  # the connect path survives a failed sweep

    assert (await reread(db_session, PrintArchive, a.archive_id)).status == "completed"
    assert prb.current_print_run(printer_manager, a.printer_id) is a.run


# --------------------------------------------------------------------------- #
# A04 / A05 / A07 / A10 -- anything that moved since the capture keeps the context
# --------------------------------------------------------------------------- #


def _processing_admission(pid):
    # A start claimed and not yet processed -- its resolution ended, so the admission's phase alone vetoes.
    prb.claim_print_start(printer_manager, pid, {"subtask_id": "111"})
    printer_manager._print_start_resolutions.pop(pid, None)


# Each of the next three moves ONE thing and keeps the effect generation: the run's own device id, no new run.
def _resolution(pid):
    prb.begin_print_start_resolution(printer_manager, pid, {"subtask_id": "111"})


def _pending_terminal(pid):
    prb.begin_print_start_resolution(printer_manager, pid, {"subtask_id": "111"})
    prb.defer_matching_terminal_during_start(printer_manager, pid, {"subtask_id": "111"})
    printer_manager._print_start_resolutions.pop(pid, None)


def _finishing_of_another_run(pid):
    run = printer_manager._print_run_bindings[pid]
    printer_manager._print_run_finishing_bindings[(pid, 777_001)] = dataclasses.replace(run, archive_id=777_001)


def _same_fields_new_object(pid):
    printer_manager._print_run_bindings[pid] = dataclasses.replace(printer_manager._print_run_bindings[pid])


def _run_b(pid):
    prb.bind_print_run(printer_manager, printer_id=pid, archive_id=888_001, origin="external")


def _reconnect(pid):
    printer_manager._client_generations[pid] = printer_manager._client_generations.get(pid, 0) + 1


def _deleted_and_readded(pid):
    printer_manager.end_printer_deletion(pid, printer_manager.begin_printer_deletion(pid))


def _restore(pid):
    from backend.app.services import queue_sources

    queue_sources.invalidate_receipts()


def _stopping(pid):
    printer_manager._print_lifecycle_stopping = True


@pytest.mark.parametrize(
    "change",
    [
        _processing_admission,
        _resolution,
        _pending_terminal,
        _finishing_of_another_run,
        _same_fields_new_object,
        _run_b,
        _reconnect,
        _deleted_and_readded,
        _restore,
        _stopping,
    ],
    ids=lambda f: f.__name__.strip("_"),
)
async def test_a_context_that_moved_during_the_sweep_is_kept(
    db_session, printer_factory, archive_factory, sweep_db, monkeypatch, change
):
    a = await a_bound_printing_run(db_session, printer_factory, archive_factory)
    real = print_reconciliation._reconcile

    left = {}

    async def sweep_then_change(db, *args, **kwargs):
        result = await real(db, *args, **kwargs)
        change(a.printer_id)
        left["binding"] = printer_manager._print_run_bindings.get(a.printer_id)
        return result

    monkeypatch.setattr(print_reconciliation, "_reconcile", sweep_then_change)

    await sweep(a)

    after = printer_manager._print_run_bindings.get(a.printer_id)
    if change is _deleted_and_readded:
        # A deletion ends the sweep before it closes anything (O1); the run is left for the deletion itself.
        assert after is a.run
    else:
        # Nothing was cleared: A, its equal-field replacement and B are exactly what the change left.
        assert after is not None and after is left["binding"]


async def test_a_start_still_processing_keeps_the_context(db_session, printer_factory, archive_factory, sweep_db):
    """A04: the run's own start, claimed and not yet processed when the sweep began, is not released under it."""
    a = await a_bound_printing_run(db_session, printer_factory, archive_factory)
    prb.claim_print_start(printer_manager, a.printer_id, {"subtask_id": "111"})
    printer_manager._print_start_resolutions.pop(a.printer_id, None)  # only the phase says it is unfinished

    await sweep(a)

    assert prb.current_print_run(printer_manager, a.printer_id) is a.run
    assert prb.start_admission(printer_manager, a.printer_id).phase == "processing"


async def test_an_unchanged_processed_admission_does_not_stop_the_release(
    db_session, printer_factory, archive_factory, sweep_db
):
    """A04 control: the dispatch's own start, processed, belongs to the run being released."""
    a = await a_bound_printing_run(db_session, printer_factory, archive_factory)
    admission = prb.claim_print_start(printer_manager, a.printer_id, {"subtask_id": "111"})
    prb.finish_print_start(printer_manager, a.printer_id, admission)

    await sweep(a)

    assert prb.current_print_run(printer_manager, a.printer_id) is None
    assert prb.start_admission(printer_manager, a.printer_id) is None


# --------------------------------------------------------------------------- #
# A08 -- once only; a second sweep and a late terminal do nothing again
# --------------------------------------------------------------------------- #


async def test_a_second_sweep_and_a_late_terminal_replay_nothing(
    db_session, printer_factory, archive_factory, sweep_db
):
    a = await a_bound_printing_run(db_session, printer_factory, archive_factory)
    await sweep(a)
    assert prb.current_print_run(printer_manager, a.printer_id) is None

    token = prb.snapshot_completion_token(printer_manager, a.printer_id)
    assert token.generation == a.run.effect_generation
    assert not prb.accept_unbound_completion(printer_manager, token)  # the late terminal of A is a duplicate

    b = prb.bind_print_run(printer_manager, printer_id=a.printer_id, archive_id=a.archive_id + 1, origin="external")
    await sweep(a)  # nothing printing any more: B is left alone
    assert prb.current_print_run(printer_manager, a.printer_id) is b
    assert b.effect_generation > a.run.effect_generation


# --------------------------------------------------------------------------- #
# A01 end to end -- the pending job behind the closed run starts on the next ordinary tick
# --------------------------------------------------------------------------- #


async def test_after_its_own_close_the_pending_job_starts_on_the_next_tick(
    db_session, test_engine, tmp_path, printer_factory, archive_factory, monkeypatch, sessions, runtime, stall_clock
):
    import backend.app.services.print_scheduler as ps
    from backend.app import main
    from backend.app.api.routes.printers import clear_plate
    from backend.tests.integration.test_dispatch_attempt_verdicts import a_job_and_its_dispatch, one_tick
    from backend.tests.integration.test_runtime_recovery_acceptance import the_printer_reports

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(main, "async_session", factory)
    monkeypatch.setattr("backend.app.core.database.async_session", factory)
    monkeypatch.setattr("backend.app.core.tasks.spawn_background_task", lambda coro, **_k: coro.close())
    queue_id, mqtt, pending_id, _service, scheduler, spawned = await a_job_and_its_dispatch(
        db_session, test_engine, tmp_path, printer_factory, monkeypatch
    )
    monkeypatch.setattr(ps, "scheduler", scheduler)
    queue = await db_session.get(PrinterQueue, queue_id)
    printer_id = queue.printer_id
    started = datetime.now(timezone.utc) - timedelta(hours=2)
    archive = await archive_factory(
        printer_id,
        queue_id=queue_id,
        filename=FILE,
        status="printing",
        started_at=started,
        completed_at=None,
        created_at=started,
        extra_data={},
    )
    item = PrintQueueItem(queue_id=queue_id, archive_id=archive.id, status="printing", started_at=started, position=0)
    db_session.add(item)
    await db_session.flush()
    queue.status = "printing"
    queue.current_item_id = item.id
    await db_session.commit()
    run = prb.bind_prepared_print_run(
        printer_manager,
        printer_id=printer_id,
        archive_id=archive.id,
        queue_item_id=item.id,
        claim_started_at=started,
        expected_submission_id="111",
    )
    assert run is not None

    mqtt.state.state = "FINISH"
    await print_reconciliation.reconcile_printer_prints(printer_id, "FINISH", FILE)

    assert prb.current_print_run(printer_manager, printer_id) is None
    queue = await reread(db_session, PrinterQueue, queue_id)
    assert queue.status == "idle" and not queue.is_paused
    await clear_plate(printer_id, None, current_user=None, db=db_session)
    the_printer_reports(mqtt, "FINISH")

    await one_tick(scheduler, spawned)

    mqtt._client.publish.assert_called_once()
    started_row = await reread(db_session, PrintQueueItem, pending_id)
    assert started_row.status == "printing", started_row.error_message
    assert prb.current_print_run(printer_manager, printer_id).archive_id == started_row.archive_id


# --------------------------------------------------------------------------- #
# A11 -- the release replays nothing and waits for no post-work
# --------------------------------------------------------------------------- #


async def test_the_release_replays_no_completion_and_waits_for_no_energy_reading(
    db_session, printer_factory, archive_factory, sweep_db, monkeypatch
):
    from unittest.mock import AsyncMock

    from backend.app import main

    a = await a_bound_printing_run(db_session, printer_factory, archive_factory)
    completion = AsyncMock()
    monkeypatch.setattr(main, "on_print_complete", completion)
    monkeypatch.setattr(main, "_on_print_complete_impl", completion)
    reading = asyncio.Event()
    energy_calls = []

    async def a_slow_plug(archive_id, printer_id, **kwargs):
        energy_calls.append(archive_id)
        await reading.wait()

    monkeypatch.setattr(main, "_record_print_energy", a_slow_plug)
    spawned = []
    monkeypatch.setattr(
        "backend.app.core.tasks.spawn_background_task",
        lambda coro, **_k: spawned.append(asyncio.ensure_future(coro)) or spawned[-1],
    )

    await asyncio.wait_for(sweep(a), timeout=30)

    assert prb.current_print_run(printer_manager, a.printer_id) is None  # released while the plug is still read
    await asyncio.sleep(0)
    assert energy_calls == [a.archive_id] and not reading.is_set()
    completion.assert_not_awaited()
    reading.set()
    await asyncio.gather(*spawned, return_exceptions=True)


# --------------------------------------------------------------------------- #
# Codex acceptance r1 -- O3-F1 / F2: swap debt for both outcomes; a missing claim start proves nothing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("state", ["FINISH", "FAILED"])
@pytest.mark.parametrize(
    "debt",
    [
        {"swap_macro_events_pending": ["swap_mode_change_table"]},
        {"swap_hold": "swap_hold_attempt_unknown"},
        {"bamdude_terminal_effects": {"swap_change_table": {"stage": "attempted", "owner": "x", "at": "t"}}},
    ],
    ids=["owed-event", "held", "attempted-claim"],
)
async def test_a_swap_still_unsettled_keeps_the_context_for_either_outcome(
    db_session, printer_factory, archive_factory, sweep_db, state, debt
):
    a = await a_bound_printing_run(db_session, printer_factory, archive_factory, extra=debt)

    await sweep(a, state)

    assert (await reread(db_session, PrintArchive, a.archive_id)).status != "printing"
    assert prb.current_print_run(printer_manager, a.printer_id) is a.run


async def test_a_settled_swap_claim_does_not_stop_the_release(db_session, printer_factory, archive_factory, sweep_db):
    """Control: a change_table that was answered is no debt."""
    debt = {"bamdude_terminal_effects": {"swap_change_table": {"stage": "finished", "owner": "x", "at": "t"}}}
    a = await a_bound_printing_run(db_session, printer_factory, archive_factory, extra=debt)

    await sweep(a, "FAILED")

    assert prb.current_print_run(printer_manager, a.printer_id) is None


async def test_a_claim_without_its_start_proves_nothing(db_session, printer_factory, archive_factory, sweep_db):
    """The same numeric archive and queue ids, no claim start and no device id: never enough to release."""
    a = await a_bound_printing_run(db_session, printer_factory, archive_factory, bind=False)
    run = prb.bind_prepared_print_run(
        printer_manager,
        printer_id=a.printer_id,
        archive_id=a.archive_id,
        queue_item_id=a.item_id,
        claim_started_at=None,
        expected_submission_id="different-run",
    )
    assert run.claim_started_at is None and run.observed_subtask_id is None

    await sweep(a)

    assert prb.current_print_run(printer_manager, a.printer_id) is run
