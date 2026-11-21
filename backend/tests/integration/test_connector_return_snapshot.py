"""B2-02: a Connector page observes one return generation and rechecks the live namespace."""

import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import connector_submissions, queue_sources
from backend.app.services.auto_queue_scheduler import AutoQueueScheduler
from backend.app.utils.failure_reasons import DISPATCH_PRINTER_UNAVAILABLE
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401
from backend.tests.integration.test_connector_observations import _observe, _publish
from backend.tests.integration.test_connector_submissions import _clean_spool_state, farm, sessions  # noqa: F401
from backend.tests.integration.test_dispatch_without_original import scheduler_with_captured_dispatch


@pytest.mark.parametrize("change", ["second_return", "restore"])
async def test_connector_observation_reads_a_consistent_return_generation(farm, sessions, monkeypatch, change):
    operation_id, submission_id = await _publish(
        farm,
        {
            "entry_id": str(uuid.uuid4()),
            "plate_id": 15,
            "quantity": 1,
            "target": {"kind": "auto", "target_model": "P1P"},
            "action": "queue_asap",
        },
    )
    router = AutoQueueScheduler()
    monkeypatch.setattr("backend.app.services.print_scheduler.async_session", sessions)
    scheduler, _ = scheduler_with_captured_dispatch(monkeypatch)

    async def place():
        async with sessions() as db:
            auto = (await db.scalars(select(AutoQueueItem))).one()
            row = await router._assign(db, auto, farm["printer"])
            return row.id

    async def refuse(job_id):
        async with sessions() as db:
            row = await db.get(PrintQueueItem, job_id)
            outcome = await scheduler._fail_item(
                db,
                row,
                "Printer unavailable",
                DISPATCH_PRINTER_UNAVAILABLE,
                storage_epoch=queue_sources.current_epoch(),
            )
            assert outcome.returned

    await refuse(await place())
    async with sessions() as db:
        header = await db.get(PrinterQueue, farm["queue"].id)
        header.status, header.is_paused = "idle", False
        await db.commit()
    second = await place()
    [before] = (await _observe(farm, operation_id, submission_id))["jobs"]
    assert before["state"] == "assigned" and before["assignment"] is not None

    paused, resume = asyncio.Event(), asyncio.Event()
    execute = AsyncSession.execute

    async def pause_after_ancestor(db, statement, *args, **kwargs):
        result = await execute(db, statement, *args, **kwargs)
        if asyncio.current_task() is reader and "auto_queue_items.recovery_identity IN" in str(statement):
            paused.set()
            await resume.wait()
        return result

    monkeypatch.setattr(AsyncSession, "execute", pause_after_ancestor)
    reader = asyncio.create_task(_observe(farm, operation_id, submission_id))
    try:
        await asyncio.wait_for(paused.wait(), 8)
        if change == "second_return":
            await asyncio.wait_for(refuse(second), 8)
            [after] = (await _observe(farm, operation_id, submission_id))["jobs"]
            assert (after["state"], after["assignment"]) == ("queued", None)
        else:
            async with sessions() as db:
                await connector_submissions.rotate_storage_epoch(db)
                await db.commit()
    finally:
        resume.set()
        await asyncio.wait_for(asyncio.gather(reader, return_exceptions=True), 10)
    if change == "restore":
        with pytest.raises(connector_submissions.ConnectorRefusal) as error:
            reader.result()
        assert (error.value.status, error.value.code) == (409, "context_changed")
    else:
        [during] = reader.result()["jobs"]
        assert during == before
        async with sessions() as db:
            auto = (await db.scalars(select(AutoQueueItem))).one()
            auto.unsent_return_proof = {**auto.unsent_return_proof, "receipt_identity": "missing-receipt"}
            await db.commit()
        [damaged] = (await _observe(farm, operation_id, submission_id))["jobs"]
        assert (damaged["state"], damaged["reason"]) == ("unavailable", "chain_broken")
