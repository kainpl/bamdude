"""Preview, apply and audit of the in-memory repair (spec printer-runtime-stall r3 R6, R8).

Two commits: the intent with the queue's pause, then revalidation, the synchronous compare-and-clear and the
outcome. The state is built by the production sweep (``a_dispatched_run_the_sweep_closes``); the printer is
synthetic. Sessions share the in-memory engine here; independent sessions are the WAL/PostgreSQL scenarios.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.print_state_recovery_action import PrintStateRecoveryAction
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import (
    print_state_recovery_preview as tokens,
    print_state_recovery_store as store,
    queue_sources,
    runtime_context_release,
)
from backend.app.services.print_run_binding import (
    accept_unbound_completion,
    bind_print_run,
    current_print_run,
    snapshot_completion_token,
)
from backend.app.services.print_state_recovery import RUNTIME_ACTION
from backend.app.services.printer_manager import printer_manager
from backend.tests.integration.test_printer_runtime_stall import (  # noqa: F401 - fixtures
    a_dispatched_run_the_sweep_closes,
    clock,
    runtime,
)
from backend.tests.unit.services.test_terminal_queue_repair import telemetry  # noqa: F401 - fixture

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture
async def a01(db_session, test_engine, printer_factory, archive_factory, telemetry, runtime, monkeypatch):
    from backend.app import main

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(main, "async_session", factory)
    printer, queue, archive, _item, pending, run = await a_dispatched_run_the_sweep_closes(
        db_session, printer_factory, archive_factory, telemetry
    )
    return SimpleNamespace(
        printer_id=printer.id, queue_id=queue.id, archive_id=archive.id, pending_id=pending.id, run=run, factory=factory
    )


@pytest.fixture
async def legacy_swap(
    db_session, test_engine, printer_factory, archive_factory, telemetry, runtime, clock, monkeypatch
):
    from backend.app import main

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(main, "async_session", factory)
    printer = await printer_factory()
    queue = PrinterQueue(printer_id=printer.id, status="idle")
    db_session.add(queue)
    await db_session.flush()
    start = datetime.now(timezone.utc) - timedelta(hours=1)
    archive = await archive_factory(
        printer.id,
        queue_id=queue.id,
        status="completed",
        dispatch_phase="accepted",
        started_at=start,
        completed_at=start - timedelta(minutes=42),
        extra_data={"swap_macro_events_pending": ["swap_mode_change_table"], "swap_start_effect": "fired"},
    )
    old = PrintQueueItem(queue_id=queue.id, archive_id=archive.id, status="failed")
    pending = PrintQueueItem(queue_id=queue.id, status="pending")
    db_session.add_all([old, pending])
    await db_session.flush()
    run = bind_print_run(
        printer_manager,
        printer_id=printer.id,
        archive_id=archive.id,
        archive_identity=archive.recovery_identity,
        queue_item_id=old.id,
        origin="dispatch",
    )
    main.register_macro_selection(printer.id, {"selected_macro_ids": []})
    await db_session.delete(old)
    await db_session.commit()
    telemetry.state = "IDLE"
    return SimpleNamespace(
        printer_id=printer.id,
        queue_id=queue.id,
        archive_id=archive.id,
        pending_id=pending.id,
        run=run,
        factory=factory,
    )


async def test_pending_swap_settlement_then_runtime_release(db_session, async_client, legacy_swap, clock, released):
    from backend.app.services.swap_terminal import operator_settlement

    a = legacy_swap
    archive = await db_session.get(PrintArchive, a.archive_id)
    outcome = (archive.status, archive.started_at, archive.completed_at)
    endpoint = f"/api/v1/system/printers/{a.printer_id}/print-state"
    issues = f"/api/v1/system/print-state-issues?printer_id={a.printer_id}"
    request = {
        "kind": "archive",
        "target_id": a.archive_id,
        "action": "settle_swap_by_operator",
        "confirmed_idle": True,
        "confirmed_manual_swap": True,
    }
    await async_client.get(issues)
    clock.advance(11)
    page = (await async_client.get(issues)).json()
    issue = next(row for row in page["issues"] if row["kind"] == "archive")
    assert issue["archive_id"] == a.archive_id and issue["actions"] == ["settle_swap_by_operator"], page
    assert (
        await async_client.post(endpoint + "/preview", json={**request, "confirmed_manual_swap": False})
    ).status_code == 409

    response = await async_client.post(endpoint + "/preview", json=request)
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["after"]["settlement"]["pending_only"] is True
    body = {"preview_token": plan["preview_token"], "confirmed": True}
    applied = await async_client.post(endpoint + "/apply", json=body)
    assert applied.status_code == 200, applied.text
    assert (await async_client.post(endpoint + "/apply", json=body)).json() == applied.json()
    assert current_print_run(printer_manager, a.printer_id) is a.run
    db_session.expire_all()
    archive = await db_session.get(PrintArchive, a.archive_id)
    assert operator_settlement(archive.extra_data, archive.recovery_identity)["previous_effect"] is None
    assert not archive.extra_data["swap_macro_events_pending"]
    assert (archive.status, archive.started_at, archive.completed_at) == outcome
    assert (await db_session.get(PrinterQueue, a.queue_id)).is_paused is True

    _response, token = await preview(db_session, a)
    assert (await apply(a, token))["outcome"] == "applied"
    assert released == [True]
    assert current_print_run(printer_manager, a.printer_id) is None
    assert (await db_session.get(PrintQueueItem, a.pending_id)).status == "pending"


@pytest.mark.parametrize("change", ["new_claim", "old_id_reused", "new_context", "valid_times", "attempted"])
async def test_pending_swap_preview_cannot_outlive_its_proof(db_session, legacy_swap, clock, change):
    a = legacy_swap
    await store.list_issues(db_session, None)
    clock.advance(11)
    plan = await store.make_preview(
        db_session,
        a.printer_id,
        "archive",
        a.archive_id,
        "settle_swap_by_operator",
        "user:1",
        confirmed_idle=True,
        confirmed_manual_swap=True,
    )
    archive = await db_session.get(PrintArchive, a.archive_id)
    if change == "new_claim":
        db_session.add(PrintQueueItem(queue_id=a.queue_id, archive_id=a.archive_id, status="printing"))
    elif change == "old_id_reused":
        db_session.add(PrintQueueItem(id=a.run.queue_item_id, queue_id=a.queue_id, status="pending"))
    elif change == "new_context":
        bind_print_run(printer_manager, printer_id=a.printer_id, archive_id=a.archive_id + 100)
    elif change == "valid_times":
        archive.completed_at = archive.started_at + timedelta(minutes=1)
    else:
        archive.extra_data = {
            **archive.extra_data,
            "bamdude_terminal_effects": {"swap_change_table": {"stage": "attempted"}},
        }
    await db_session.commit()
    with pytest.raises(ValueError, match="recovery_changed"):
        await apply(a, tokens.verify(plan["preview_token"]))
    assert (await db_session.get(PrinterQueue, a.queue_id)).is_paused is False


@pytest.fixture
def released(monkeypatch):
    """Every compare-and-clear the protocol attempts, and its answer."""
    calls = []
    real = runtime_context_release.release

    def spy(manager, expected):
        result = real(manager, expected)
        calls.append(result)
        return result

    monkeypatch.setattr(runtime_context_release, "release", spy)
    return calls


async def preview(db, a):
    response = await store.make_preview(db, a.printer_id, "runtime", a.archive_id, RUNTIME_ACTION, "user:1")
    return response, tokens.verify(response["preview_token"])


async def apply(a, payload, *, recheck=None):
    return await store.apply_preview(a.factory, payload, "user:1", "Operator", recheck=recheck)


async def rows(db):
    db.expire_all()
    return list(
        (await db.scalars(select(PrintStateRecoveryAction).order_by(PrintStateRecoveryAction.created_at))).all()
    )


def outcome_id(nonce):
    return str(uuid5(NAMESPACE_URL, f"bamdude:print-state-recovery:outcome:{nonce}"))


async def test_the_preview_says_what_goes_and_what_stays(db_session, a01):
    response, _ = await preview(db_session, a01)
    assert response["action"] == RUNTIME_ACTION and response["archive_preserved"] is True
    assert response["after"]["queue"] == {"is_paused": True}
    assert response["after"]["runtime"] == {
        "archive_id": a01.archive_id,
        "admission": False,
        "macro_selection": True,
        "swap_config": False,
    }
    assert response["runtime"]["queue_paused"] is False
    assert response["before"]["queue"]["is_paused"] is False


async def test_apply_releases_the_context_pauses_the_queue_and_audits_twice(db_session, a01, released):
    from backend.app import main

    _, payload = await preview(db_session, a01)
    archive_before = await db_session.get(PrintArchive, a01.archive_id)
    snapshot = (archive_before.status, archive_before.completed_at, dict(archive_before.extra_data))

    result = await apply(a01, payload)

    assert result["outcome"] == "applied" and result["operation_id"] == payload["nonce"]
    assert released == [True]
    assert current_print_run(printer_manager, a01.printer_id) is None
    assert a01.printer_id not in main._active_macro_selection
    # A late terminal of A is a duplicate now; nothing of A replays.
    assert (
        accept_unbound_completion(printer_manager, snapshot_completion_token(printer_manager, a01.printer_id)) is False
    )
    db_session.expire_all()
    queue = await db_session.get(PrinterQueue, a01.queue_id)
    assert queue.is_paused is True and queue.status == "idle"
    archive = await db_session.get(PrintArchive, a01.archive_id)
    assert (archive.status, archive.completed_at, archive.extra_data) == snapshot
    assert (await db_session.get(PrintQueueItem, a01.pending_id)).status == "pending"
    intent, outcome = await rows(db_session)
    assert intent.id == payload["nonce"] and intent.result["phase"] == "intent"
    assert outcome.id == outcome_id(payload["nonce"]) and outcome.result["phase"] == "outcome"
    assert outcome.result["operation_id"] == payload["nonce"] and outcome.result["outcome"] == "applied"
    assert intent.result["before"]["queue_was_paused"] is False
    assert intent.result["incarnation"] == tokens.incarnation()


async def test_the_same_token_never_clears_twice(db_session, a01, released):
    _, payload = await preview(db_session, a01)
    first = await apply(a01, payload)
    again = await apply(a01, payload)
    assert again == first and released == [True]
    assert len(await rows(db_session)) == 2


async def test_a_failed_intent_commit_changes_nothing(db_session, a01, released):
    _, payload = await preview(db_session, a01)

    def fail(_mapper, _connection, target):
        if target.result.get("phase") == "intent":
            raise RuntimeError("audit unavailable")

    event.listen(PrintStateRecoveryAction, "before_insert", fail)
    try:
        with pytest.raises(RuntimeError, match="audit unavailable"):
            await apply(a01, payload)
    finally:
        event.remove(PrintStateRecoveryAction, "before_insert", fail)
    assert released == [] and current_print_run(printer_manager, a01.printer_id) is a01.run
    db_session.expire_all()
    assert (await db_session.get(PrinterQueue, a01.queue_id)).is_paused is False
    assert await rows(db_session) == []


async def test_a_run_bound_after_the_intent_is_left_alone_and_the_pause_stays(db_session, a01, released):
    """A07 / A19: B appears between the two commits; the old CAS never touches it."""
    _, payload = await preview(db_session, a01)
    holder = {}

    def b_starts(_mapper, _connection, target):
        if target.result.get("phase") == "intent":
            holder["b"] = bind_print_run(
                printer_manager, printer_id=a01.printer_id, archive_id=a01.archive_id + 50, observed_subtask_id="999"
            )

    event.listen(PrintStateRecoveryAction, "after_insert", b_starts)
    try:
        result = await apply(a01, payload)
    finally:
        event.remove(PrintStateRecoveryAction, "after_insert", b_starts)
    assert result["outcome"] == "not_applied_changed" and result["queue_paused"] is True
    assert released in ([], [False])
    assert current_print_run(printer_manager, a01.printer_id) is holder["b"]
    db_session.expire_all()
    assert (await db_session.get(PrinterQueue, a01.queue_id)).is_paused is True
    intent, outcome = await rows(db_session)
    assert outcome.result["outcome"] == "not_applied_changed"


async def test_an_unconfirmed_outcome_is_reported_and_never_retried(db_session, a01, released):
    _, payload = await preview(db_session, a01)

    def fail(_mapper, _connection, target):
        if target.result.get("phase") == "outcome":
            raise RuntimeError("outcome lost")

    event.listen(PrintStateRecoveryAction, "before_insert", fail)
    try:
        result = await apply(a01, payload)
        again = await apply(a01, payload)
    finally:
        event.remove(PrintStateRecoveryAction, "before_insert", fail)
    assert result["outcome"] == "outcome_unconfirmed" and result["operation_id"] == payload["nonce"]
    assert again["outcome"] == "outcome_unconfirmed"
    assert released == [True]  # the context went once; the repeat did not try again
    assert current_print_run(printer_manager, a01.printer_id) is None
    (intent,) = await rows(db_session)
    assert intent.result["phase"] == "intent"


async def test_rights_revoked_after_the_intent_keep_the_pause(db_session, a01, released):
    from fastapi import HTTPException

    _, payload = await preview(db_session, a01)
    checks = 0

    async def revoked_later(_action, _db):
        nonlocal checks
        checks += 1
        if checks >= 3:  # preflight, the intent's writer -- then gone
            raise HTTPException(403, "recovery_changed")

    result = await apply(a01, payload, recheck=revoked_later)
    assert result["outcome"] == "not_applied_changed" and result["reason"] == "rights_changed"
    assert released == [] and current_print_run(printer_manager, a01.printer_id) is a01.run
    db_session.expire_all()
    assert (await db_session.get(PrinterQueue, a01.queue_id)).is_paused is True


async def test_while_a_preview_lives_no_new_object_takes_an_identity_it_named(db_session, a01, released):
    """Minor 2: the preview names the run by object identity. A rebind (``replace()``) with the same content
    must stay a different object for as long as the preview can be applied -- the old one cannot be freed and
    its id handed to a newcomer."""
    from dataclasses import replace as dc_replace

    _, payload = await preview(db_session, a01)
    named = id(a01.run)
    a01.run = None
    seen = set()
    for _ in range(50):
        newcomer = dc_replace(printer_manager._print_run_bindings[a01.printer_id])
        printer_manager._print_run_bindings[a01.printer_id] = newcomer
        seen.add(id(newcomer))
    assert named not in seen
    # Seen before the first commit: refused whole, nothing paused, nothing written.
    with pytest.raises(ValueError, match="recovery_changed"):
        await apply(a01, payload)
    assert released == [] and await rows(db_session) == []
    assert current_print_run(printer_manager, a01.printer_id) is newcomer


async def test_a_failure_before_the_cas_is_recorded_as_not_applied(db_session, a01, released, monkeypatch):
    """Minor 1: the guards of the second transaction fail -- memory was never touched, and history says so."""
    _, payload = await preview(db_session, a01)
    real = store._take_guards
    calls = 0

    async def second_fails(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("database is locked")
        return await real(*args, **kwargs)

    monkeypatch.setattr(store, "_take_guards", second_fails)
    result = await apply(a01, payload)
    assert result["outcome"] == "not_applied_changed" and result["reason"] == "unavailable"
    assert released == [] and current_print_run(printer_manager, a01.printer_id) is a01.run
    intent, outcome = await rows(db_session)
    assert outcome.id == outcome_id(payload["nonce"])
    assert outcome.result["outcome"] == "not_applied_changed" and outcome.result["reason"] == "unavailable"
    db_session.expire_all()
    assert (await db_session.get(PrinterQueue, a01.queue_id)).is_paused is True
    # The repeat reads the recorded answer.
    again = await apply(a01, payload)
    assert again["outcome"] == "not_applied_changed" and again["reason"] == "unavailable"


async def test_a_database_error_in_the_recheck_is_not_called_lost_rights(db_session, a01, released):
    _, payload = await preview(db_session, a01)
    checks = 0

    async def broken_later(_action, _db):
        nonlocal checks
        checks += 1
        if checks >= 3:
            raise RuntimeError("database is locked")

    result = await apply(a01, payload, recheck=broken_later)
    assert result["outcome"] == "not_applied_changed" and result["reason"] == "unavailable"
    assert released == [] and current_print_run(printer_manager, a01.printer_id) is a01.run


async def test_the_row_says_which_repair_paused_its_queue(db_session, a01, released, clock):
    """Minor 6 / R8.6, A26: after a reload the row itself says the pause came from a repair, and how it ended."""
    from fastapi import HTTPException

    _, payload = await preview(db_session, a01)
    checks = 0

    async def revoked_later(_action, _db):
        nonlocal checks
        checks += 1
        if checks >= 3:
            raise HTTPException(403, "recovery_changed")

    await apply(a01, payload, recheck=revoked_later)
    await store.list_issues(db_session, None)
    clock.advance(11)
    page = await store.list_issues(db_session, None)
    (row,) = [r for r in page["issues"] if r["printer_id"] == a01.printer_id and r["kind"] == "runtime"]
    assert row["queue_paused"] is True
    repair = row["last_repair"]
    assert (repair["outcome"], repair["reason"]) == ("not_applied_changed", "rights_changed")
    assert repair["archive_id"] == a01.archive_id and repair["queue_was_paused"] is False
    assert repair["process_ended"] is False and repair["at"]


def _repair_rows(page, printer_id):
    return [r for r in page["issues"] if r["printer_id"] == printer_id and r["kind"] == "repair"]


async def test_a_released_context_still_explains_its_pause_after_a_reload(db_session, a01, clock):
    """RS-R3: the context is gone and the queue stays paused -- the printer's row still says why, read for read,
    and as an observation Health does not count."""
    _, payload = await preview(db_session, a01)
    assert (await apply(a01, payload))["outcome"] == "applied"
    first = await store.list_issues(db_session, None)
    clock.advance(20)
    reloaded = await store.list_issues(db_session, None)
    for page in (first, reloaded):
        (row,) = _repair_rows(page, a01.printer_id)
        assert row["code"] == "queue_paused_by_repair" and row["observation"] is True and row["actions"] == []
        assert row["queue_paused"] is True
        repair = row["last_repair"]
        assert (repair["outcome"], repair["archive_id"], repair["queue_was_paused"]) == (
            "applied",
            a01.archive_id,
            False,
        )
    assert not [r for r in reloaded["issues"] if r["printer_id"] == a01.printer_id and r["kind"] == "runtime"]


async def test_the_explanation_goes_with_the_operators_resume(db_session, a01, clock):
    from backend.app.api.routes.printer_queues import update_queue
    from backend.app.schemas.printer_queue import PrinterQueueUpdate

    _, payload = await preview(db_session, a01)
    await apply(a01, payload)
    assert _repair_rows(await store.list_issues(db_session, None), a01.printer_id)
    await update_queue(a01.queue_id, PrinterQueueUpdate(is_paused=False), db=db_session, _=None)
    assert _repair_rows(await store.list_issues(db_session, None), a01.printer_id) == []


async def test_an_unrecorded_release_still_explains_the_pause_and_says_its_result_is_unknown(
    db_session, a01, clock, monkeypatch
):
    _, payload = await preview(db_session, a01)

    def fail(_mapper, _connection, target):
        if target.result.get("phase") == "outcome":
            raise RuntimeError("outcome lost")

    event.listen(PrintStateRecoveryAction, "before_insert", fail)
    try:
        assert (await apply(a01, payload))["outcome"] == "outcome_unconfirmed"
    finally:
        event.remove(PrintStateRecoveryAction, "before_insert", fail)
    (row,) = _repair_rows(await store.list_issues(db_session, None), a01.printer_id)
    assert row["last_repair"]["outcome"] is None and row["last_repair"]["process_ended"] is False
    monkeypatch.setattr(tokens, "_INCARNATION", "after-a-restart")
    (row,) = _repair_rows(await store.list_issues(db_session, None), a01.printer_id)
    assert row["last_repair"]["process_ended"] is True


async def _synthetic_attempts(db, printer_id, queue_id, *, count, start, outcome, archive_id=None):
    """Valid audit pairs of this action for ``printer_id``, ``start`` onwards, a second apart."""
    for n in range(count):
        nonce = str(uuid4())
        for phase in ("intent", "outcome"):
            result = {"phase": phase, "before": {"queue_was_paused": True}, "incarnation": "synthetic"}
            if phase == "outcome":
                result.update(outcome=outcome, operation_id=nonce, reason="context_changed")
            db.add(
                PrintStateRecoveryAction(
                    id=nonce if phase == "intent" else store.outcome_id(nonce),
                    created_at=start + timedelta(seconds=n, microseconds=0 if phase == "intent" else 1),
                    actor="user:1",
                    actor_label="Operator",
                    printer_id=printer_id,
                    queue_id=queue_id,
                    archive_id=archive_id,
                    action=RUNTIME_ACTION,
                    input_hash="h",
                    result=result,
                )
            )
    await db.commit()


async def test_another_printers_history_never_hides_a_pause_explanation(db_session, a01, printer_factory, test_engine):
    """RS2-R1: each printer's latest attempt is chosen on its own -- a busy history elsewhere cannot crowd it out,
    and the read stays a fixed number of statements however long the histories are."""
    _, payload = await preview(db_session, a01)
    assert (await apply(a01, payload))["outcome"] == "applied"
    other = await printer_factory()
    queue = PrinterQueue(printer_id=other.id, status="idle", is_paused=True)
    db_session.add(queue)
    await db_session.flush()
    later = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
    await _synthetic_attempts(db_session, other.id, queue.id, count=60, start=later, outcome="not_applied_changed")
    statements = []

    def count(_conn, _cursor, statement, *_args):
        if "print_state_recovery_actions" in statement:
            statements.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", count)
    try:
        page = await store.list_issues(db_session, None)
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", count)
    shown = {r["printer_id"]: r for r in page["issues"] if r["kind"] == "repair"}
    assert set(shown) == {a01.printer_id, other.id}, page
    assert shown[a01.printer_id]["last_repair"]["outcome"] == "applied"
    assert len(statements) <= 3, statements  # the candidates' EXISTS, the latest intents, their outcomes


async def test_the_latest_of_several_attempts_is_shown_with_its_own_outcome(db_session, a01):
    earlier = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    await _synthetic_attempts(
        db_session, a01.printer_id, a01.queue_id, count=3, start=earlier, outcome="not_applied_changed", archive_id=7
    )
    _, payload = await preview(db_session, a01)
    assert (await apply(a01, payload))["outcome"] == "applied"
    (row,) = _repair_rows(await store.list_issues(db_session, None), a01.printer_id)
    assert (row["last_repair"]["outcome"], row["last_repair"]["archive_id"]) == ("applied", a01.archive_id)


async def test_a_queue_paused_by_hand_gets_no_repair_row(db_session, a01):
    queue = await db_session.get(PrinterQueue, a01.queue_id)
    queue.is_paused = True
    await db_session.commit()
    assert _repair_rows(await store.list_issues(db_session, None), a01.printer_id) == []


async def test_a_restore_between_the_commits_keeps_the_pause(db_session, a01, released):
    _, payload = await preview(db_session, a01)

    def restored(_mapper, _connection, target):
        if target.result.get("phase") == "intent":
            queue_sources.invalidate_receipts()

    event.listen(PrintStateRecoveryAction, "after_insert", restored)
    try:
        result = await apply(a01, payload)
    finally:
        event.remove(PrintStateRecoveryAction, "after_insert", restored)
    assert result["outcome"] == "not_applied_changed" and released == []


async def test_a_restore_before_apply_writes_nothing(db_session, a01, released):
    _, payload = await preview(db_session, a01)
    queue_sources.invalidate_receipts()
    with pytest.raises(ValueError, match="recovery_changed"):
        await apply(a01, payload)
    assert await rows(db_session) == [] and released == []


@pytest.mark.parametrize(("status", "paused"), [("idle", False), ("error", False), ("idle", True)])
async def test_only_the_pause_flag_changes_and_a_prior_pause_is_named(db_session, a01, status, paused):
    """A26: the queue's status is never written; a pause set before the repair is recorded and shown."""
    queue = await db_session.get(PrinterQueue, a01.queue_id)
    queue.status, queue.is_paused = status, paused
    await db_session.commit()
    response, payload = await preview(db_session, a01)
    assert response["runtime"]["queue_paused"] is paused
    result = await apply(a01, payload)
    assert result["outcome"] == "applied" and result["queue_was_paused"] is paused
    db_session.expire_all()
    queue = await db_session.get(PrinterQueue, a01.queue_id)
    assert (queue.status, queue.is_paused) == (status, True)


async def test_the_plate_gate_is_neither_armed_nor_cleared(db_session, a01):
    """A21: the sweep armed it; the repair leaves it exactly as it was."""
    armed = printer_manager.is_awaiting_plate_clear(a01.printer_id)
    _, payload = await preview(db_session, a01)
    await apply(a01, payload)
    assert printer_manager.is_awaiting_plate_clear(a01.printer_id) is armed


async def test_a_cancelled_request_still_ends_the_operation(db_session, a01, released):
    """R8.8: the client went away after the intent; the operation finishes and its outcome is recorded."""
    _, payload = await preview(db_session, a01)
    committed = asyncio.Event()

    def mark(_mapper, _connection, target):
        if target.result.get("phase") == "intent":
            committed.set()

    event.listen(PrintStateRecoveryAction, "after_insert", mark)
    try:
        request = asyncio.ensure_future(apply(a01, payload))
        await asyncio.wait_for(committed.wait(), 10)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        for _ in range(500):
            if len(await rows(db_session)) == 2:
                break
            await asyncio.sleep(0.01)
    finally:
        event.remove(PrintStateRecoveryAction, "after_insert", mark)
    intent, outcome = await rows(db_session)
    assert outcome.result["outcome"] == "applied" and released == [True]


async def test_shutdown_waits_for_an_operation_whose_client_went_away(db_session, a01, released):
    """Minor 7: the shielded operation is drained at shutdown, so its outcome is written before the process ends."""
    _, payload = await preview(db_session, a01)
    committed = asyncio.Event()

    def mark(_mapper, _connection, target):
        if target.result.get("phase") == "intent":
            committed.set()

    event.listen(PrintStateRecoveryAction, "after_insert", mark)
    try:
        request = asyncio.ensure_future(apply(a01, payload))
        await asyncio.wait_for(committed.wait(), 10)
        request.cancel()
        assert await store.drain_runtime_operations(timeout=10) == 0
    finally:
        event.remove(PrintStateRecoveryAction, "after_insert", mark)
    intent, outcome = await rows(db_session)
    assert outcome.result["outcome"] == "applied" and released == [True]


async def test_a_failure_nobody_waits_for_is_still_logged(db_session, a01, monkeypatch, caplog):
    """Minor 7: ``asyncio.shield`` quietly takes the exception of an operation whose client left -- so the
    operation reports its own failure, or it would vanish without a trace."""
    import logging

    _, payload = await preview(db_session, a01)
    gate = asyncio.Event()

    async def broken(*_args, **_kwargs):
        await gate.wait()
        raise RuntimeError("database went away")

    monkeypatch.setattr(store, "_apply_runtime", broken)
    request = asyncio.ensure_future(apply(a01, payload))
    await asyncio.sleep(0)
    request.cancel()
    with caplog.at_level(logging.WARNING, logger=store.__name__):
        gate.set()
        assert await store.drain_runtime_operations(timeout=10) == 0
    assert any("operation failed" in r.getMessage() for r in caplog.records), caplog.records


async def test_history_groups_the_pair_and_names_an_ended_process(async_client, db_session, a01, monkeypatch):
    _, payload = await preview(db_session, a01)
    await apply(a01, payload)
    response = await async_client.get(f"/api/v1/system/printers/{a01.printer_id}/print-state/history")
    assert response.status_code == 200, response.text
    (action,) = response.json()["actions"]
    assert action["id"] == payload["nonce"] and action["outcome"]["outcome"] == "applied"
    assert action["process_ended"] is False


async def test_history_reads_the_outcomes_of_a_page_at_once(async_client, db_session, test_engine, a01):
    """Minor 9: one read of the page and one of its outcomes, however many repairs the page holds."""
    for n in range(12):
        db_session.add(
            store._audit_row(
                f"00000000-0000-4000-8000-{n:012d}",
                "user:1",
                "Operator",
                (a01.printer_id, "runtime", a01.archive_id),
                a01.queue_id,
                a01.archive_id,
                "h",
                {"id": f"00000000-0000-4000-8000-{n:012d}", "phase": "intent", "action": RUNTIME_ACTION},
            )
        )
    await db_session.commit()
    statements = []

    def count(_conn, _cursor, statement, *_args):
        if "print_state_recovery_actions" in statement:
            statements.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", count)
    try:
        response = await async_client.get(f"/api/v1/system/printers/{a01.printer_id}/print-state/history")
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", count)
    assert response.status_code == 200 and len(response.json()["actions"]) == 12
    assert len(statements) <= 2, statements


async def test_an_intent_without_outcome_of_an_ended_process_proves_nothing(async_client, db_session, a01, monkeypatch):
    _, payload = await preview(db_session, a01)

    def fail(_mapper, _connection, target):
        if target.result.get("phase") == "outcome":
            raise RuntimeError("outcome lost")

    event.listen(PrintStateRecoveryAction, "before_insert", fail)
    try:
        await apply(a01, payload)
    finally:
        event.remove(PrintStateRecoveryAction, "before_insert", fail)
    url = f"/api/v1/system/printers/{a01.printer_id}/print-state/history"
    (action,) = (await async_client.get(url)).json()["actions"]
    assert action["outcome"] is None and action["process_ended"] is False
    monkeypatch.setattr(tokens, "_INCARNATION", "another-process")
    (action,) = (await async_client.get(url)).json()["actions"]
    assert action["outcome"] is None and action["process_ended"] is True


async def test_http_preview_and_apply_need_queue_update(async_client, db_session, a01):
    base = f"/api/v1/system/printers/{a01.printer_id}/print-state"
    body = {"kind": "runtime", "target_id": a01.archive_id, "action": RUNTIME_ACTION}
    response = await async_client.post(base + "/preview", json=body)
    assert response.status_code == 200, response.text
    applied = await async_client.post(
        base + "/apply", json={"preview_token": response.json()["preview_token"], "confirmed": True}
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["outcome"] == "applied"
    made = await async_client.post(
        "/api/v1/api-keys/", json={"name": "status", "can_queue": False, "can_read_status": True}
    )
    async_client.headers.pop("Authorization", None)
    refused = await async_client.post(base + "/preview", json=body, headers={"X-API-Key": made.json()["key"]})
    assert refused.status_code == 403, refused.text


async def test_an_open_timelapse_of_the_run_keeps_it(db_session, a01, monkeypatch, clock):
    """RS-R2: the layer timelapse is looked up by printer alone -- released, A's session would film the next
    print. Until its own safe handling exists it holds the run: shown, named, no repair."""
    from backend.app.services import layer_timelapse

    session = SimpleNamespace(archive_id=a01.archive_id, capture_layer=AsyncMock())
    monkeypatch.setattr(layer_timelapse, "_active_sessions", {a01.printer_id: session})
    with pytest.raises(ValueError, match="recovery_not_allowed"):
        await preview(db_session, a01)
    await store.list_issues(db_session, None)
    clock.advance(11)
    page = await store.list_issues(db_session, None)
    (row,) = [r for r in page["issues"] if r["printer_id"] == a01.printer_id and r["kind"] == "runtime"]
    assert row["actions"] == [] and "timelapse_open" in row["blocked_reasons"]
    assert current_print_run(printer_manager, a01.printer_id) is a01.run
    session.capture_layer.assert_not_awaited()


async def test_a_null_swap_carrier_keeps_the_swap_registration(db_session, a01):
    """RS-R1: ``swap_macro_events_pending: null`` is not "nothing owed" -- no preview, the swap config stays."""
    from backend.app import main

    archive = await db_session.get(PrintArchive, a01.archive_id)
    archive.extra_data = {**(archive.extra_data or {}), "swap_macro_events_pending": None}
    await db_session.commit()
    main.register_swap_config(
        a01.printer_id, {"execute_swap_macros": True, "swap_macro_events": ["swap_mode_change_table"]}
    )
    with pytest.raises(ValueError, match="recovery_not_allowed"):
        await preview(db_session, a01)
    assert a01.printer_id in main._active_swap_config
    assert current_print_run(printer_manager, a01.printer_id) is a01.run
    assert await db_session.scalar(select(func.count()).select_from(PrintStateRecoveryAction)) == 0


async def test_a_swap_still_owed_has_no_preview(db_session, a01):
    """A05 / A23: the obligation is kept -- nothing to release, nothing paused, nothing written."""
    archive = await db_session.get(PrintArchive, a01.archive_id)
    archive.extra_data = {
        **(archive.extra_data or {}),
        "bamdude_terminal_effects": {"swap_change_table": {"stage": "owed"}},
    }
    await db_session.commit()
    with pytest.raises(ValueError, match="recovery_not_allowed"):
        await preview(db_session, a01)
    assert await db_session.scalar(select(func.count()).select_from(PrintStateRecoveryAction)) == 0
