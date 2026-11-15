"""The reset writer preserves physical history and publishes exactly once."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.auto_queue_return import AutoQueueReturn
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.settings import Settings
from backend.app.models.user import User
from backend.app.services import auto_queue_return as returns
from backend.tests.integration.swap_terminal_support import an_execution, complete, make_rig  # noqa: F401
from backend.tests.integration.test_auto_queue_return_settlement import failed_auto_run  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def ready(run):
    rig, _, _, _, _ = run
    await complete(rig, {"status": "failed", "subtask_id": "81"})
    async with rig.db() as db:
        receipt = await db.scalar(select(AutoQueueReturn))
        assert receipt is not None
        return receipt.id, receipt.completed_at + timedelta(seconds=120)


async def test_two_connections_publish_once_and_retain_failed_owner(failed_auto_run):
    rig, archive_id, job_id, auto_id, _ = failed_auto_run
    receipt_id, now = await ready(failed_auto_run)
    async with rig.db() as db:
        job = await db.get(PrintQueueItem, job_id)
        archive = await db.get(PrintArchive, archive_id)
        auto = await db.get(AutoQueueItem, auto_id)
        queue = await db.get(PrinterQueue, rig.queue_id)
        printer = await db.get(Printer, rig.printer_id)
        old = (
            job.error_code,
            job.error_message,
            archive.failure_reason,
            archive.extra_data,
            queue.status,
            queue.is_paused,
            printer.awaiting_plate_clear,
            printer.awaiting_plate_clear_archive_id,
        )
        identity, snapshot, position = auto.recovery_identity, auto.source_snapshot, auto.position
    left, right = await asyncio.gather(
        returns.return_ready(receipt_id, now=now), returns.return_ready(receipt_id, now=now)
    )
    assert left == right and left.ordinal == 1
    async with rig.db() as db:
        job = await db.get(PrintQueueItem, job_id)
        archive = await db.get(PrintArchive, archive_id)
        auto = await db.get(AutoQueueItem, auto_id)
        queue = await db.get(PrinterQueue, rig.queue_id)
        printer = await db.get(Printer, rig.printer_id)
        assert (
            job.error_code,
            job.error_message,
            archive.failure_reason,
            archive.extra_data,
            queue.status,
            queue.is_paused,
            printer.awaiting_plate_clear,
            printer.awaiting_plate_clear_archive_id,
        ) == old
        assert job.status == archive.status == "failed"
        assert job.returned_receipt_identity == receipt_id and job.source_auto_item_id is None
        assert job.source_auto_identity == auto.recovery_identity == identity
        assert job.queue_source_id == auto.queue_source_id and job.source_snapshot == auto.source_snapshot == snapshot
        assert auto.position == position and auto.status == "pending"
        assert auto.failed_return_count == 1 and auto.failed_return_proof["receipt_identity"] == receipt_id
        assert auto.assigned_at is None and auto.assigned_to_item_id is None
        assert len((await db.scalars(select(PrintQueueItem))).all()) == 1
        assert len((await db.scalars(select(PrintArchive))).all()) == 1
    assert await returns.return_ready(receipt_id, now=now) == left
    async with rig.db() as db:
        from backend.app.api.routes.auto_queue import get_auto_queue_item
        from backend.app.api.routes.print_queue import get_queue_item, queue_issues

        shown = await get_queue_item(job_id, db=db, auth_result=(None, True))
        assert shown.returned_receipt_identity == receipt_id and shown.return_ordinal == 1
        assert shown.return_refusal_code is None
        issues = await queue_issues(queue_id=rig.queue_id, cursor=None, limit=50, db=db, auth_result=(None, True))
        assert issues.items[0].return_ordinal == 1
        assert issues.items[0].returned_receipt_identity == receipt_id
        live = await get_auto_queue_item(auto_id, db=db, _=None)
        assert live.failed_return_count == 1 and live.return_refusal_code is None


@pytest.mark.parametrize(
    "mode",
    [
        "cooldown",
        "off",
        "owner",
        "policy",
        "limit",
        "cancel",
        "reassign",
        "swap_attempted",
        "swap_owed",
        "future_clock",
        "direct",
        "calibration",
        "malformed_claim",
    ],
)
async def test_refusal_is_atomic_and_does_not_consume_budget(failed_auto_run, mode):
    rig, archive_id, job_id, auto_id, _ = failed_auto_run
    receipt_id, at = await ready(failed_auto_run)
    async with rig.db() as db:
        auto = await db.get(AutoQueueItem, auto_id)
        if mode == "cooldown":
            at -= timedelta(microseconds=1)
        elif mode == "future_clock":
            at -= timedelta(days=1)
        elif mode == "off":
            (await db.scalar(select(Settings).where(Settings.key == returns.SETTING_KEY))).value = "false"
        elif mode == "owner":
            (await db.get(User, auto.created_by_id)).is_active = False
        elif mode == "policy":
            auto.force_color_match = not auto.force_color_match
        elif mode == "limit":
            auto.failed_return_count = 2
        elif mode == "cancel":
            auto.status = "cancelled"
        elif mode == "reassign":
            auto.assigned_to_item_id = job_id + 100
        elif mode == "direct":
            (await db.get(PrintQueueItem, job_id)).origin = "direct"
        elif mode == "calibration":
            (await db.get(PrintQueueItem, job_id)).is_calibration = True
        else:
            archive = await db.get(PrintArchive, archive_id)
            archive.extra_data = {
                **archive.extra_data,
                "bamdude_terminal_effects": {"swap_change_table": {"stage": mode.removeprefix("swap_")}},
            }
            if mode == "malformed_claim":
                archive.extra_data = {**archive.extra_data, "bamdude_terminal_effects": {"swap_change_table": []}}
        await db.commit()
    with pytest.raises(returns.ReturnRefused):
        await returns.return_ready(receipt_id, now=at)
    async with rig.db() as db:
        auto = await db.get(AutoQueueItem, auto_id)
        assert auto.failed_return_count == (2 if mode == "limit" else 0)
        assert auto.failed_return_proof is None
        assert (await db.get(PrintQueueItem, job_id)).returned_receipt_identity is None
        assert (await db.get(AutoQueueReturn, receipt_id)).returned_at is None


@pytest.mark.parametrize("committed", [True, False])
async def test_unknown_commit_rereads_without_compensation(failed_auto_run, monkeypatch, committed):
    rig, _, job_id, auto_id, _ = failed_auto_run
    receipt_id, now = await ready(failed_auto_run)
    commit = AsyncSession.commit
    calls = []

    async def uncertain(self):
        if not calls:
            calls.append(1)
            if committed:
                await commit(self)
            raise RuntimeError("lost acknowledgement")
        await commit(self)

    monkeypatch.setattr(AsyncSession, "commit", uncertain)
    if committed:
        result = await returns.return_ready(receipt_id, now=now)
        assert result.ordinal == 1
    else:
        with pytest.raises(RuntimeError):
            await returns.return_ready(receipt_id, now=now)
    async with rig.db() as db:
        assert (await db.get(AutoQueueItem, auto_id)).failed_return_count == int(committed)
        assert ((await db.get(PrintQueueItem, job_id)).returned_receipt_identity is not None) == committed
    result = await returns.return_ready(receipt_id, now=now)
    assert result.ordinal == 1


async def test_two_returns_then_third_physical_failure_stays_failed(failed_auto_run):
    rig, _, first_job_id, auto_id, _ = failed_auto_run
    receipt_id, at = await ready(failed_auto_run)
    first = await returns.return_ready(receipt_id, now=at)
    assert first.ordinal == 1
    async with rig.db() as db:
        first_job = await db.get(PrintQueueItem, first_job_id)
        retained = {
            key: getattr(first_job, key)
            for key in ("queue_source_id", "source_snapshot", "filament_routing", "created_by_id")
        }
    for expected in (2, 3):
        archive_id, job_id = await an_execution(rig, fact=False, name=f"attempt-{expected}")
        async with rig.db() as db:
            auto = await db.get(AutoQueueItem, auto_id)
            job = await db.get(PrintQueueItem, job_id)
            for field, value in retained.items():
                setattr(job, field, value)
            job.source_auto_item_id, job.source_auto_identity = auto.id, auto.recovery_identity
            job.dispatch_attempts = 1
            auto.status, auto.assigned_to_item_id = "assigned", job.id
            archive = await db.get(PrintArchive, archive_id)
            archive.queue_item_identity = job.recovery_identity
            archive.source_auto_identity = auto.recovery_identity
            archive.created_by_id = job.created_by_id
            await db.commit()
        await complete(rig, {"status": "failed", "subtask_id": "81"})
        async with rig.db() as db:
            receipt = await db.scalar(select(AutoQueueReturn).where(AutoQueueReturn.old_job_id == job_id))
            if expected == 3:
                # The terminal print is retained, but an exhausted logical job
                # must not add another ready receipt to the background pass.
                assert receipt is None
            else:
                assert receipt is not None
                receipt_id, at = receipt.id, receipt.completed_at + timedelta(seconds=120)
        if expected == 2:
            assert (await returns.return_ready(receipt_id, now=at)).ordinal == 2
    async with rig.db() as db:
        auto = await db.get(AutoQueueItem, auto_id)
        assert auto.failed_return_count == 2 and auto.status == "assigned"
        assert auto.assigned_to_item_id == job_id
        assert (await db.get(PrintQueueItem, job_id)).status == "failed"
        assert len((await db.scalars(select(PrintArchive))).all()) == 3
        assert (
            len((await db.scalars(select(AutoQueueReturn).where(AutoQueueReturn.returned_at.is_not(None)))).all()) == 2
        )


async def test_failed_swap_hold_returns_without_clearing_old_physical_debt(failed_auto_run):
    from backend.app.services import swap_terminal

    rig, archive_id, job_id, auto_id, _ = failed_auto_run
    async with rig.db() as db:
        (await db.get(Printer, rig.printer_id)).swap_mode_enabled = True
        (await db.get(PrintQueueItem, job_id)).execute_swap_macros = True
        archive = await db.get(PrintArchive, archive_id)
        archive.extra_data = {**archive.extra_data, "swap_macro_events_pending": [swap_terminal.CHANGE_TABLE_EVENT]}
        await db.commit()
    receipt_id, now = await ready(failed_auto_run)
    async with rig.db() as db:
        archive = await db.get(PrintArchive, archive_id)
        queue = await db.get(PrinterQueue, rig.queue_id)
        assert archive.extra_data["swap_hold"]["reason"] == swap_terminal.HOLD_NOT_COMPLETED
        assert queue.status == "paused"
        operator_pause = queue.is_paused
        old = archive.extra_data
    assert (await returns.return_ready(receipt_id, now=now)).ordinal == 1
    async with rig.db() as db:
        assert (await db.get(AutoQueueItem, auto_id)).status == "pending"
        assert (await db.get(PrintQueueItem, job_id)).status == "failed"
        assert (await db.get(PrintArchive, archive_id)).extra_data == old
        queue = await db.get(PrinterQueue, rig.queue_id)
        assert queue.status == "paused" and queue.is_paused == operator_pause
    rig.macro.assert_not_awaited()


@pytest.mark.parametrize("unavailable", [False, True])
async def test_return_uses_accepted_bytes_after_original_is_gone(failed_auto_run, tmp_path, unavailable):
    from backend.app.models.library import LibraryFile
    from backend.app.models.queue_source import QueueSource

    rig, _, job_id, auto_id, _ = failed_auto_run
    async with rig.db() as db:
        original = LibraryFile(
            filename="original.3mf", file_path=str(tmp_path / "missing-original.3mf"), file_type="3mf", file_size=999
        )
        db.add(original)
        await db.flush()
        (await db.get(PrintQueueItem, job_id)).library_file_id = original.id
        (await db.get(AutoQueueItem, auto_id)).library_file_id = original.id
        await db.commit()
    receipt_id, at = await ready(failed_auto_run)
    async with rig.db() as db:
        job = await db.get(PrintQueueItem, job_id)
        auto = await db.get(AutoQueueItem, auto_id)
        job.library_file_id = auto.library_file_id = None
        await db.delete(await db.get(LibraryFile, original.id))
        source = await db.get(QueueSource, job.queue_source_id)
        blob_path = tmp_path / source.relative_path
        assert blob_path.is_relative_to(tmp_path)
        if unavailable:
            blob_path.write_bytes(b"corrupted accepted bytes")
        await db.commit()
    if unavailable:
        with pytest.raises(returns.ReturnRefused, match="source_unavailable"):
            await returns.return_ready(receipt_id, now=at)
    else:
        assert (await returns.return_ready(receipt_id, now=at)).ordinal == 1
    async with rig.db() as db:
        auto = await db.get(AutoQueueItem, auto_id)
        assert auto.failed_return_count == int(not unavailable)
        assert auto.status == ("assigned" if unavailable else "pending")
