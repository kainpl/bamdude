"""Operator close through HTTP and cancellation, with real database writers."""

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import Session

from backend.app.models.print_state_recovery_action import PrintStateRecoveryAction
from backend.app.services import (
    print_state_recovery_preview as tokens,
    print_state_recovery_store as store,
    runtime_context_release,
    usage_tracker,
)
from backend.app.services.print_run_binding import bind_print_run, current_print_run
from backend.app.services.printer_manager import printer_manager
from backend.tests.integration.test_print_state_recovery import prepare_stuck_print
from backend.tests.unit.services.test_terminal_queue_repair import telemetry  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.mark.parametrize("outcome,status", [("interrupted", "failed"), ("unknown", "cancelled")])
async def test_operator_close_http_preserves_history_and_gate(
    async_client, db_session, printer_factory, archive_factory, telemetry, monkeypatch, outcome, status
):
    (printer, queue, archive, item, pending), tick = await prepare_stuck_print(
        db_session, printer_factory, archive_factory, monkeypatch
    )
    await async_client.get("/api/v1/system/print-state-issues")
    tick[0] += 11
    path = f"/api/v1/system/printers/{printer.id}/print-state"
    preview = await async_client.post(
        path + "/preview",
        json={
            "kind": "item",
            "target_id": item.id,
            "action": "close_interrupted_print",
            "confirmed_idle": True,
            "operator_outcome": outcome,
        },
    )
    assert preview.status_code == 200, preview.text
    request = {"preview_token": preview.json()["preview_token"], "confirmed": True}
    applied = await async_client.post(path + "/apply", json=request)
    assert applied.status_code == 200, applied.text
    repeated = await async_client.post(path + "/apply", json=request)
    assert applied.json()["context_released"] is True
    assert repeated.status_code == 200
    assert repeated.json() == {key: value for key, value in applied.json().items() if key != "context_released"}
    history = await async_client.get(path + "/history")
    assert history.status_code == 200
    assert history.json()["actions"][0]["operator_outcome"] == outcome
    assert "context_released" not in history.json()["actions"][0]
    for row in (printer, queue, archive, item, pending):
        await db_session.refresh(row)
    assert archive.status == item.status == status
    assert queue.is_paused and queue.current_item_id is None
    assert printer.awaiting_plate_clear_token == "existing-gate"
    assert pending.status == "pending"


async def test_cancelled_request_does_not_detach_operator_close(
    db_session, test_engine, printer_factory, archive_factory, telemetry, monkeypatch
):
    (printer, queue, archive, item, _), tick = await prepare_stuck_print(
        db_session, printer_factory, archive_factory, monkeypatch
    )
    await store.list_issues(db_session, None)
    tick[0] += 11
    preview = await store.make_preview(
        db_session,
        printer.id,
        "item",
        item.id,
        "close_interrupted_print",
        "user:1",
        confirmed_idle=True,
        operator_outcome="interrupted",
    )
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def recheck(_action, _db):
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            await release.wait()

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    payload = tokens.verify(preview["preview_token"])
    waiter = asyncio.create_task(store.apply_preview(factory, payload, "user:1", "Operator", recheck=recheck))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        owned = set(store._runtime_operations)
        assert len(owned) == 1
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert all(not operation.done() for operation in owned)
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*store._runtime_operations), 5)
        if not waiter.done():
            await waiter
    for row in (queue, archive, item):
        await db_session.refresh(row)
    assert queue.is_paused and queue.current_item_id is None
    assert archive.status == item.status == "failed"
    assert await db_session.scalar(select(func.count()).select_from(PrintStateRecoveryAction)) == 1
    result = await store.apply_preview(factory, payload, "user:1", "Operator")
    assert result["id"] == payload["nonce"]


@pytest.mark.parametrize("with_context", [True, False])
@pytest.mark.parametrize("session_owner", ["closed", "successor", "unstamped"])
async def test_close_drops_only_its_usage_session_after_commit(
    db_session, test_engine, printer_factory, archive_factory, telemetry, monkeypatch, with_context, session_owner
):
    (printer, _, archive, item, _), tick = await prepare_stuck_print(
        db_session, printer_factory, archive_factory, monkeypatch, with_context=with_context
    )
    identity = {"closed": archive.recovery_identity, "successor": "another-execution", "unstamped": None}[session_owner]
    session = usage_tracker.PrintSession(printer.id, "stopped", datetime.now(timezone.utc), archive_identity=identity)
    monkeypatch.setattr(usage_tracker, "_active_sessions", {printer.id: session, -1: session})
    # Durable start evidence stays with its owner, not this process-only cleanup.
    await usage_tracker.persist_session(db_session, session)
    await store.list_issues(db_session, None)
    tick[0] += 11
    preview = await store.make_preview(
        db_session,
        printer.id,
        "item",
        item.id,
        "close_interrupted_print",
        "user:1",
        confirmed_idle=True,
        operator_outcome="interrupted",
    )

    class CommitProof(Session):
        pass

    @event.listens_for(CommitProof, "after_commit")
    def before_release(_session):
        assert usage_tracker._active_sessions[printer.id] is session

    factory = async_sessionmaker(test_engine, expire_on_commit=False, sync_session_class=CommitProof)
    result = await store.apply_preview(factory, tokens.verify(preview["preview_token"]), "user:1", "Operator")
    assert result["context_released"] is True
    assert (printer.id in usage_tracker._active_sessions) == (session_owner != "closed")
    assert usage_tracker._active_sessions[-1] is session
    if session_owner != "closed":
        assert usage_tracker._active_sessions[printer.id] is session
    from backend.app.models.active_print_session import ActivePrintSession
    from backend.app.models.spool_usage_history import SpoolUsageHistory

    assert (await db_session.get(ActivePrintSession, printer.id)).archive_identity == identity
    assert await db_session.scalar(select(func.count()).select_from(SpoolUsageHistory)) == 0


@pytest.mark.parametrize("release", ["error", "refused", "successor"])
async def test_postcommit_release_failure_does_not_hide_committed_close(
    async_client, db_session, test_engine, printer_factory, archive_factory, telemetry, monkeypatch, caplog, release
):
    (printer, _, archive, item, _), tick = await prepare_stuck_print(
        db_session, printer_factory, archive_factory, monkeypatch
    )
    session = usage_tracker.PrintSession(
        printer.id, "stopped", datetime.now(timezone.utc), archive_identity=archive.recovery_identity
    )
    sessions = {printer.id: session}
    monkeypatch.setattr(usage_tracker, "_active_sessions", sessions)
    await store.list_issues(db_session, None)
    tick[0] += 11
    path = f"/api/v1/system/printers/{printer.id}/print-state"
    preview = await async_client.post(
        path + "/preview",
        json={
            "kind": "item",
            "target_id": item.id,
            "action": "close_interrupted_print",
            "confirmed_idle": True,
            "operator_outcome": "interrupted",
        },
    )
    assert preview.status_code == 200, preview.text
    held = current_print_run(printer_manager, printer.id)
    calls = []

    def unavailable(*_args):
        calls.append(True)
        if release == "error":
            raise RuntimeError("secret diagnostic must not escape")
        return False

    if release == "successor":

        class CommitSuccessor(Session):
            pass

        @event.listens_for(CommitSuccessor, "after_commit")
        def install_successor(_session):
            bind_print_run(printer_manager, printer_id=printer.id, archive_id=archive.id + 1000)
            sessions[printer.id] = usage_tracker.PrintSession(
                printer.id, "new", datetime.now(timezone.utc), archive_identity="new-execution"
            )

        factory = async_sessionmaker(test_engine, expire_on_commit=False, sync_session_class=CommitSuccessor)
        payload = tokens.verify(preview.json()["preview_token"])
        result = await store.apply_preview(factory, payload, payload["actor"], "Local operator")
        assert current_print_run(printer_manager, printer.id).archive_id == archive.id + 1000
        assert sessions[printer.id].archive_identity == "new-execution"
    else:
        monkeypatch.setattr(runtime_context_release, "release", unavailable)
        request = {"preview_token": preview.json()["preview_token"], "confirmed": True}
        applied = await async_client.post(path + "/apply", json=request)
        assert applied.status_code == 200, applied.text
        result = applied.json()
        repeated = await async_client.post(path + "/apply", json=request)
        assert repeated.status_code == 200
        assert "context_released" not in repeated.json()
        assert calls == [True]  # Replay never attempts another release.
        assert current_print_run(printer_manager, printer.id) is held
        assert sessions[printer.id] is session
    assert result["context_released"] is False
    assert "secret diagnostic" not in caplog.text
    if release == "error":
        assert "context release unavailable" in caplog.text and "RuntimeError" in caplog.text
    for row in (archive, item):
        await db_session.refresh(row)
        assert row.status == "failed"
    audit = await db_session.get(PrintStateRecoveryAction, result["id"])
    assert "context_released" not in audit.result
    assert await db_session.scalar(select(func.count()).select_from(PrintStateRecoveryAction)) == 1
