"""Positive settlement comes from the real live callback, never terminal age."""

import asyncio
import hashlib
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from backend.app import main
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.auto_queue_return import AutoQueueReturn
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.settings import Settings
from backend.app.models.user import User
from backend.app.services import auto_queue_return as returns, queue_sources
from backend.app.services.auto_queue_return_runtime import AutoQueueReturnRuntime
from backend.app.services.filament_policy import serialize_policy
from backend.app.services.filament_routing import RoutingPolicy
from backend.app.services.print_run_binding import current_print_effect_token
from backend.tests.integration.swap_terminal_support import (  # noqa: F401
    an_execution,
    complete,
    give_job_a_snapshot,
    make_rig,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def failed_auto_run(make_rig, tmp_path, monkeypatch):
    rig = await make_rig(require_plate_clear=True, swap_mode=False)
    archive_id, job_id = await an_execution(rig, fact=False)
    blob = b"; accepted immutable print bytes\n"
    sha = hashlib.sha256(blob).hexdigest()
    await give_job_a_snapshot(rig, job_id, fact=False, sha=sha)
    monkeypatch.setattr(main.app_settings, "base_dir", tmp_path)
    from backend.app.models.queue_source import QueueSource

    async with rig.db() as db:
        user = User(username="return-owner", role="admin", is_active=True)
        db.add(user)
        await db.flush()
        job = await db.get(PrintQueueItem, job_id)
        source = await db.get(QueueSource, job.queue_source_id)
        source.size_bytes = len(blob)
        path = tmp_path / source.relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        auto = AutoQueueItem(
            status="assigned",
            assigned_to_item_id=job.id,
            created_by_id=user.id,
            queue_source_id=job.queue_source_id,
            source_snapshot=job.source_snapshot,
            target_model="P1S",
            position=3,
        )
        db.add(auto)
        await db.flush()
        job.created_by_id = user.id
        job.source_auto_item_id = auto.id
        job.source_auto_identity = auto.recovery_identity
        job.dispatch_attempts = 1
        job.filament_routing = serialize_policy(RoutingPolicy(), queue_source_id=source.id)
        archive = await db.get(PrintArchive, archive_id)
        archive.queue_item_identity = job.recovery_identity
        archive.source_auto_identity = auto.recovery_identity
        archive.created_by_id = user.id
        db.add(Settings(key=returns.SETTING_KEY, value="true"))
        await db.commit()
        auto_id = auto.id
    runtime = AutoQueueReturnRuntime()
    runtime.open_admission()
    monkeypatch.setattr("backend.app.services.auto_queue_return_runtime.auto_queue_return_runtime", runtime)
    yield rig, archive_id, job_id, auto_id, runtime
    assert await runtime.quiesce_for_restore(timeout=5)


async def test_real_failed_callback_publishes_ready_without_rearming(failed_auto_run):
    rig, archive_id, job_id, auto_id, _ = failed_auto_run
    await complete(rig, {"status": "failed", "subtask_id": "81"})
    async with rig.db() as db:
        receipts = (await db.scalars(select(AutoQueueReturn))).all()
        assert len(receipts) == 1
        receipt = receipts[0]
        job = await db.get(PrintQueueItem, job_id)
        auto = await db.get(AutoQueueItem, auto_id)
        archive = await db.get(PrintArchive, archive_id)
        assert receipt.old_job_identity == job.recovery_identity
        assert receipt.execution_archive_identity == archive.recovery_identity
        assert receipt.completed_at == job.completed_at == archive.completed_at
        assert receipt.returned_at is None and receipt.ordinal is None
        assert job.status == archive.status == "failed"
        assert job.returned_receipt_identity is None
        assert auto.status == "assigned" and auto.assigned_to_item_id == job.id
        assert auto.failed_return_count == 0 and auto.failed_return_proof is None
    await complete(rig, {"status": "failed", "subtask_id": "81"})
    async with rig.db() as db:
        assert len((await db.scalars(select(AutoQueueReturn))).all()) == 1


@pytest.mark.parametrize("mode", ["off", "ownerless", "not_started", "direct", "cancelled", "completed"])
async def test_noneligible_completion_never_mints_ready(failed_auto_run, mode):
    rig, _, job_id, _, _ = failed_auto_run
    async with rig.db() as db:
        job = await db.get(PrintQueueItem, job_id)
        if mode == "off":
            setting = await db.scalar(select(Settings).where(Settings.key == returns.SETTING_KEY))
            setting.value = "false"
        elif mode == "ownerless":
            job.created_by_id = None
        elif mode == "not_started":
            job.dispatch_attempts = 0
        elif mode == "direct":
            job.source_auto_item_id = None
        await db.commit()
    await complete(rig, {"status": mode if mode in {"cancelled", "completed"} else "failed", "subtask_id": "81"})
    async with rig.db() as db:
        assert (await db.scalars(select(AutoQueueReturn))).all() == []


@pytest.mark.parametrize("mode", ["refused", "raised"])
async def test_late_archive_writer_failure_is_not_normal_success(failed_auto_run, monkeypatch, mode):
    rig, _, _, _, _ = failed_auto_run
    writer = AsyncMock(return_value=False)
    if mode == "raised":
        writer.side_effect = RuntimeError("private database text")
    monkeypatch.setattr("backend.app.services.archive.ArchiveService.update_archive_status", writer)
    await complete(rig, {"status": "failed", "subtask_id": "81"})
    assert writer.await_count == 1
    async with rig.db() as db:
        assert (await db.scalars(select(AutoQueueReturn))).all() == []


@pytest.mark.parametrize("boundary", ["queue", "plate", "retire", "tail_cancel"])
async def test_critical_failure_or_cancel_never_publishes(failed_auto_run, monkeypatch, boundary):
    from backend.app.services.queue_leftovers import RetireResult

    rig, _, _, _, _ = failed_auto_run
    if boundary == "queue":
        monkeypatch.setattr(
            "backend.app.services.queue_counters.update_queue_counters", AsyncMock(side_effect=RuntimeError("write"))
        )
    elif boundary == "plate":
        monkeypatch.setattr(rig.manager, "arm_awaiting_plate_clear", AsyncMock(side_effect=RuntimeError("write")))
    elif boundary == "retire":
        monkeypatch.setattr(
            "backend.app.services.queue_leftovers.retire_for_printer", AsyncMock(return_value=RetireResult(failed=True))
        )
    else:
        monkeypatch.setattr(
            "backend.app.services.queue_leftovers.retire_for_printer", AsyncMock(side_effect=asyncio.CancelledError())
        )
    if boundary == "tail_cancel":
        with pytest.raises(asyncio.CancelledError):
            await complete(rig, {"status": "failed", "subtask_id": "81"})
    else:
        await complete(rig, {"status": "failed", "subtask_id": "81"})
    async with rig.db() as db:
        assert (await db.scalars(select(AutoQueueReturn))).all() == []


async def test_ready_publication_rechecks_independent_transaction(failed_auto_run, monkeypatch):
    rig, archive_id, job_id, _, runtime = failed_auto_run
    runtime.close_admission()
    token = current_print_effect_token(rig.manager, rig.printer_id)
    await complete(rig, {"status": "failed", "subtask_id": "81"})
    async with rig.db() as db:
        candidate = await returns.capture_candidate(
            db,
            await db.get(PrintQueueItem, job_id),
            await db.get(PrintArchive, archive_id),
            epoch=queue_sources.current_epoch(),
            effect_token=token,
        )
    assert candidate is not None
    with pytest.raises(returns.ReturnRefused, match="storage_changed"):
        await returns.publish_ready(replace(candidate, epoch=candidate.epoch - 1), manager=rig.manager)
    real_preflight = returns.preflight

    async def changed_after_read(value):
        await real_preflight(value)
        async with rig.db() as db:
            row = await db.get(PrintQueueItem, job_id)
            row.recovery_identity = "replacement-incarnation"
            await db.commit()

    monkeypatch.setattr(returns, "preflight", changed_after_read)
    with pytest.raises(returns.ReturnRefused, match="attempt_changed"):
        await returns.publish_ready(candidate, manager=rig.manager)
    async with rig.db() as db:
        assert (await db.scalars(select(AutoQueueReturn))).all() == []


async def test_cancelled_waiter_does_not_detach_owned_io():
    import threading

    runtime = AutoQueueReturnRuntime()
    runtime.open_admission()
    started, release = threading.Event(), threading.Event()

    def io():
        started.set()
        assert release.wait(5)

    caller = asyncio.create_task(runtime.run(lambda: asyncio.to_thread(io)))
    while not started.is_set():
        await asyncio.sleep(0)
    caller.cancel()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert len(runtime.tasks) == 1
    assert not await runtime.quiesce_for_restore(timeout=0.01)
    assert not runtime.admitting
    release.set()
    assert await runtime.quiesce_for_restore(timeout=5)
    await asyncio.sleep(0)
    assert runtime.tasks == set()
