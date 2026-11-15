"""Retired physical rows stay visible but cannot become a second runnable attempt."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import plate_hold, print_execution
from backend.app.services.printer_status_context import printers_with_waiting_rows
from backend.tests.integration.test_auto_queue_return_schema import foundation_engine  # noqa: F401
from backend.tests.integration.test_duplicate_repeat_consumers import _held_print

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
CODE = "already_returned_to_auto_queue"


@pytest.fixture
async def retired(db_session, printer_factory):
    printer = await printer_factory()
    queue = PrinterQueue(id=printer.id, printer_id=printer.id, status="paused")
    db_session.add(queue)
    await db_session.flush()
    row = PrintQueueItem(
        queue_id=queue.id,
        status="failed",
        position=0,
        error_code="physical_failure",
        error_message="original failure",
        returned_receipt_identity="missing-but-retired",
        batch_id="returned-batch",
    )
    db_session.add(row)
    await db_session.commit()
    return row


@pytest.mark.parametrize("action", ["retry", "clone", "bulk", "batch"])
async def test_http_retry_clone_bulk_batch_refuse_without_writes(async_client, db_session, retired, action):
    sibling = PrintQueueItem(queue_id=retired.queue_id, status="pending", position=1, batch_id=retired.batch_id)
    db_session.add(sibling)
    await db_session.commit()
    before = await db_session.scalar(select(func.count(PrintQueueItem.id)))
    if action == "bulk":
        response = await async_client.post(
            "/api/v1/queue/bulk-retry", json={"queue_id": retired.queue_id, "item_ids": [retired.id]}
        )
    elif action == "batch":
        response = await async_client.post(f"/api/v1/queue/batch/{retired.batch_id}/clone")
    else:
        response = await async_client.post(f"/api/v1/queue/{retired.id}/{action}")
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == CODE
    await db_session.refresh(retired)
    await db_session.refresh(sibling)
    assert (retired.status, retired.error_code, retired.error_message) == (
        "failed",
        "physical_failure",
        "original failure",
    )
    assert sibling.status == "pending"
    assert await db_session.scalar(select(func.count(PrintQueueItem.id))) == before


@pytest.mark.parametrize("marker", ["missing-receipt", ""])
async def test_missing_or_empty_receipt_fails_closed(db_session, retired, marker):
    retired.returned_receipt_identity = marker
    await db_session.commit()
    with pytest.raises(print_execution.ReturnedJobRepeat) as error:
        await print_execution.require_repeatable(db_session, retired)
    assert error.value.reason == CODE


async def test_whole_batch_refuses_retired_sibling(db_session, retired):
    ordinary = PrintQueueItem(queue_id=retired.queue_id, status="pending", batch_id=retired.batch_id)
    db_session.add(ordinary)
    await db_session.commit()
    await print_execution.require_repeatable(db_session, ordinary)
    with pytest.raises(print_execution.ReturnedJobRepeat):
        await print_execution.require_repeatable(db_session, ordinary, whole_batch=True)


async def test_batch_guard_refreshes_sibling_marked_by_another_transaction(foundation_engine):
    factory = async_sessionmaker(foundation_engine, expire_on_commit=False)
    async with factory() as reader:
        ordinary = PrintQueueItem(queue_id=2, status="pending", batch_id="identity-map-race")
        sibling = PrintQueueItem(queue_id=2, status="failed", batch_id="identity-map-race")
        reader.add_all([ordinary, sibling])
        await reader.commit()
        assert sibling.returned_receipt_identity is None
        async with factory() as writer:
            changed = await writer.get(PrintQueueItem, sibling.id)
            changed.returned_receipt_identity = "committed-in-another-transaction"
            await writer.commit()
        with pytest.raises(print_execution.ReturnedJobRepeat):
            await print_execution.require_repeatable(reader, ordinary, whole_batch=True)
        assert sibling.returned_receipt_identity == "committed-in-another-transaction"


@pytest.mark.parametrize("marker", [None, "retired", ""])
@pytest.mark.parametrize("code", [None, "physical_failure", "noncurrent_execution_claim"])
async def test_sql_and_in_memory_repeatability_agree(db_session, retired, marker, code):
    retired.returned_receipt_identity = marker
    retired.error_code = code
    await db_session.commit()
    actual = set(
        (await db_session.scalars(select(PrintQueueItem.id).where(print_execution.repeatable_predicate()))).all()
    )
    assert (retired.id in actual) == (print_execution.repeat_refusal_code(retired) is None)


async def test_repeat_hidden_but_clear_keeps_addressed_owner(db_session, printer_factory, archive_factory):
    printer, queue, archive, library = await _held_print(db_session, printer_factory, archive_factory)
    archive.status = "failed"
    row = PrintQueueItem(
        queue_id=queue.id,
        archive_id=archive.id,
        library_file_id=library.id,
        status="failed",
        error_code="physical_failure",
        completed_at=datetime.now(timezone.utc),
        returned_receipt_identity="retired",
    )
    db_session.add(row)
    await db_session.commit()
    assert await plate_hold.waiting_row(db_session, printer.id) is row
    assert await plate_hold.plate_gate_owner_ids(db_session, [row.id]) == {row.id}
    assert not await plate_hold.has_waiting_row(db_session, printer.id)
    assert await printers_with_waiting_rows(db_session, [printer.id]) == set()
    with pytest.raises(print_execution.ReturnedJobRepeat):
        await plate_hold.answer_by_repeating(db_session, printer.id, expected_archive_id=archive.id)
    assert await plate_hold.answer_by_clearing(db_session, printer.id, expected_archive_id=archive.id) == 0
    await db_session.refresh(row)
    assert row.status == "failed" and row.error_code == "physical_failure"
    assert await db_session.get(Printer, printer.id) is not None


@pytest.mark.parametrize("state", ["pending", "assigned"])
async def test_delete_old_returned_row_does_not_delete_new_ancestor(async_client, db_session, retired, state):
    auto = AutoQueueItem(
        target_model="P1S", status=state, failed_return_count=1, failed_return_proof={"receipt_identity": "retired"}
    )
    db_session.add(auto)
    await db_session.flush()
    retired.source_auto_item_id = None
    retired.source_auto_identity = auto.recovery_identity
    if state == "assigned":
        successor = PrintQueueItem(queue_id=retired.queue_id, status="pending", position=1)
        db_session.add(successor)
        await db_session.flush()
        auto.assigned_to_item_id = successor.id
    await db_session.commit()
    response = await async_client.delete(f"/api/v1/queue/{retired.id}")
    assert response.status_code == 200, response.text
    assert response.json() == {"message": "Queue item deleted"}
    await db_session.refresh(auto)
    assert auto.status == state and auto.failed_return_count == 1
    assert auto.failed_return_proof == {"receipt_identity": "retired"}
    if state == "assigned":
        assert auto.assigned_to_item_id == successor.id
        assert await db_session.get(PrintQueueItem, successor.id, populate_existing=True) is not None
