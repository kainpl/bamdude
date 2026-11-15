"""Permanent refusals cost no repeated hash/lane/writer work; off costs reads only."""

from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from backend.app.core import database
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.auto_queue_return import AutoQueueReturn
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.project import Project
from backend.app.models.settings import Settings
from backend.app.services import auto_queue_return as returns, queue_sources
from backend.app.services.auto_queue_return_runtime import AutoQueueReturnRuntime
from backend.tests.integration.swap_terminal_support import make_rig  # noqa: F401
from backend.tests.integration.test_auto_queue_return_publication import ready
from backend.tests.integration.test_auto_queue_return_runtime import mature
from backend.tests.integration.test_auto_queue_return_settlement import failed_auto_run  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

FINAL_CODES = (
    "return_limit",
    "attempt_changed",
    "execution_changed",
    "history_changed",
    "settlement_changed",
    "settlement_missing",
    "source_or_policy_changed",
    "order_changed",
)


@pytest.mark.parametrize("code", FINAL_CODES)
async def test_final_refusals_are_not_selected_even_after_restart(failed_auto_run, monkeypatch, code):
    rig, _, _, _, _ = failed_auto_run
    receipt_id, _ = await ready(failed_auto_run)
    await mature(rig, receipt_id)
    async with rig.db() as db:
        (await db.get(AutoQueueReturn, receipt_id)).last_refusal_code = code
        await db.commit()
    work = AsyncMock(side_effect=AssertionError("Final refusal selected again"))
    writer = AsyncMock(side_effect=AssertionError("No writer for retired receipts"))
    monkeypatch.setattr(returns, "return_ready", work)
    monkeypatch.setattr(database, "take_write_lock", writer)
    runtime = AutoQueueReturnRuntime()
    runtime.open_admission()
    for _ in range(4):
        await runtime.pass_ready()
    # A stale in-flight transient result cannot revive an already-final receipt.
    await runtime._diagnose(receipt_id, "source_unavailable", queue_sources.current_epoch())
    work.assert_not_awaited()
    writer.assert_not_awaited()
    assert await runtime.quiesce_for_restore()


async def test_competing_commit_during_cheap_reads_keeps_duplicate_result(failed_auto_run, monkeypatch):
    receipt_id, at = await ready(failed_auto_run)
    original = returns._precheck_return
    results = []

    async def competing_commit(db, candidate):
        # The outer call has already read an unreturned receipt. The other
        # connection commits before it reads the marker/ancestor.
        monkeypatch.setattr(returns, "_precheck_return", original)
        results.append(await returns.return_ready(receipt_id, now=at))
        await original(db, candidate)

    monkeypatch.setattr(returns, "_precheck_return", competing_commit)
    result = await returns.return_ready(receipt_id, now=at)
    assert result == results[0] and result.ordinal == 1


@pytest.mark.parametrize("mode", ["limit", "retried", "deleted", "marked", "later", "closed_order"])
async def test_impossible_attempts_skip_hash_and_lanes_then_stop_work(failed_auto_run, monkeypatch, mode):
    rig, archive_id, job_id, auto_id, runtime = failed_auto_run
    if mode == "closed_order":
        async with rig.db() as db:
            order = Project(name="return order")
            db.add(order)
            await db.flush()
            (await db.get(AutoQueueItem, auto_id)).project_id = order.id
            (await db.get(PrintQueueItem, job_id)).project_id = order.id
            order_id = order.id
            await db.commit()
    receipt_id, _ = await ready(failed_auto_run)
    await mature(rig, receipt_id)
    async with rig.db() as db:
        if mode == "limit":
            (await db.get(AutoQueueItem, auto_id)).failed_return_count = 2
        elif mode == "retried":
            (await db.get(PrintQueueItem, job_id)).status = "pending"
        elif mode == "deleted":
            await db.delete(await db.get(PrintQueueItem, job_id))
        elif mode == "marked":
            (await db.get(PrintQueueItem, job_id)).returned_receipt_identity = "another-receipt"
        elif mode == "later":
            old = await db.get(PrintArchive, archive_id)
            db.add(
                PrintArchive(
                    printer_id=old.printer_id,
                    filename="later-print.3mf",
                    file_path=old.file_path,
                    file_size=old.file_size,
                    started_at=old.completed_at + timedelta(seconds=1),
                )
            )
        else:
            (await db.get(Project, order_id)).status = "completed"
        await db.commit()
    preflight = AsyncMock(side_effect=AssertionError("Cheap refusal must precede preflight"))
    lanes = Mock(side_effect=AssertionError("Cheap refusal must precede lane guard"))
    monkeypatch.setattr(returns, "preflight", preflight)
    monkeypatch.setattr(returns, "claim_lanes", lanes)
    await runtime.pass_ready()  # Persist the final reason once.
    async with rig.db() as db:
        row = await db.get(AutoQueueReturn, receipt_id)
        assert row.last_refusal_code in FINAL_CODES
        assert row.returned_at is None
        episode_at = row.last_refusal_at
    writer = AsyncMock(side_effect=AssertionError("Repeated final refusal must do no writes"))
    monkeypatch.setattr(database, "take_write_lock", writer)
    for _ in range(4):
        await runtime.pass_ready()
    preflight.assert_not_awaited()
    lanes.assert_not_called()
    writer.assert_not_awaited()
    async with rig.db() as db:
        assert (await db.get(AutoQueueReturn, receipt_id)).last_refusal_at == episode_at


async def test_off_does_not_read_receipts_hash_or_write_and_enable_resumes(failed_auto_run, monkeypatch):
    from sqlalchemy import event

    rig, _, _, auto_id, runtime = failed_auto_run
    receipt_id, _ = await ready(failed_auto_run)
    await mature(rig, receipt_id)
    async with rig.db() as db:
        (await db.scalar(select(Settings).where(Settings.key == returns.SETTING_KEY))).value = "false"
        await db.commit()
        engine = db.bind.sync_engine
    statements = []

    def record(conn, cursor, statement, *args):
        statements.append(statement.lower())

    with monkeypatch.context() as patch:
        writer = AsyncMock(side_effect=AssertionError("Off must not take writer"))
        preflight = AsyncMock(side_effect=AssertionError("Off must not hash"))
        patch.setattr(database, "take_write_lock", writer)
        patch.setattr(returns, "preflight", preflight)
        event.listen(engine, "before_cursor_execute", record)
        try:
            for _ in range(4):
                await runtime.pass_ready()
        finally:
            event.remove(engine, "before_cursor_execute", record)
        writer.assert_not_awaited()
        preflight.assert_not_awaited()
    assert not any("auto_queue_returns" in statement for statement in statements)
    assert not any(
        statement.lstrip().startswith(("update", "insert", "delete", "begin immediate")) for statement in statements
    )
    async with rig.db() as db:
        assert (await db.get(AutoQueueReturn, receipt_id)).last_refusal_code is None
        (await db.scalar(select(Settings).where(Settings.key == returns.SETTING_KEY))).value = "true"
        await db.commit()
    await runtime.pass_ready()
    async with rig.db() as db:
        assert (await db.get(AutoQueueItem, auto_id)).failed_return_count == 1


async def test_transient_refusal_retries_without_rewriting_same_episode(failed_auto_run, monkeypatch):
    rig, _, _, auto_id, runtime = failed_auto_run
    receipt_id, _ = await ready(failed_auto_run)
    await mature(rig, receipt_id)
    with monkeypatch.context() as patch:
        patch.setattr(returns, "return_ready", AsyncMock(side_effect=returns.ReturnRefused("source_unavailable")))
        await runtime.pass_ready()
        writer = AsyncMock(side_effect=AssertionError("Same transient diagnostic needs no writer"))
        patch.setattr(database, "take_write_lock", writer)
        for _ in range(4):
            await runtime.pass_ready()
        writer.assert_not_awaited()
    for _ in range(2):
        await runtime.pass_ready()
    async with rig.db() as db:
        assert (await db.get(AutoQueueItem, auto_id)).failed_return_count == 1
        assert (await db.get(AutoQueueReturn, receipt_id)).last_refusal_code is None
