"""Operator close through HTTP and cancellation, with real database writers."""

import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.print_state_recovery_action import PrintStateRecoveryAction
from backend.app.services import print_state_recovery_preview as tokens, print_state_recovery_store as store
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
    assert repeated.status_code == 200 and repeated.json() == applied.json()
    history = await async_client.get(path + "/history")
    assert history.status_code == 200
    assert history.json()["actions"][0]["operator_outcome"] == outcome
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
