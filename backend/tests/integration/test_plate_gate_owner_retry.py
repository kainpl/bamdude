"""The row a plate gate waits on is answered on the printer, never re-armed by Retry.

Incident 2026-11-11 (P1S "1st"): an aborted print left its queue row ``cancelled`` with the
plate gate armed on its archive. Retry put that row back to ``pending`` with the same archive,
and from then on every Clear plate / Repeat print answered 409 while the queue could not start
(spec plate-gate-owner-requeue r1, A1-A5, T1-T5).
"""

import pytest

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.tests.integration.test_plate_answers_defects import _finished_flat, _finished_printer

pytestmark = pytest.mark.integration


async def owned_gate(db_session, printer_factory, status="cancelled"):
    """A finished row whose archive owns the printer's armed plate gate."""
    printer = await printer_factory()
    archive, row = await _finished_flat(db_session, printer, 1)
    archive.status = row.status = status
    printer.awaiting_plate_clear = True
    printer.awaiting_plate_clear_archive_id = archive.id
    printer.awaiting_plate_clear_token = "owned-gate"
    await db_session.commit()
    return printer.id, archive.id, row.id


async def gate_of(db_session, printer_id):
    db_session.expire_all()
    printer = await db_session.get(Printer, printer_id)
    return printer.awaiting_plate_clear, printer.awaiting_plate_clear_archive_id


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["cancelled", "failed"])
async def test_retry_refuses_the_row_the_plate_gate_waits_on(async_client, printer_factory, db_session, status):
    pid, aid, row_id = await owned_gate(db_session, printer_factory, status)

    response = await async_client.post(f"/api/v1/queue/{row_id}/retry")

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"] == "plate_answer_required"
    db_session.expire_all()
    row = await db_session.get(PrintQueueItem, row_id)
    assert (row.status, row.archive_id) == (status, aid)
    assert await gate_of(db_session, pid) == (True, aid)


@pytest.mark.asyncio
async def test_bulk_retry_leaves_the_gate_owner_and_retries_the_rest(async_client, printer_factory, db_session):
    pid, aid, row_id = await owned_gate(db_session, printer_factory)
    row = await db_session.get(PrintQueueItem, row_id)
    other = PrintQueueItem(queue_id=row.queue_id, status="failed", position=5, library_file_id=1)
    db_session.add(other)
    await db_session.commit()
    queue_id, other_id = row.queue_id, other.id

    response = await async_client.post(
        "/api/v1/queue/bulk-retry",
        json={"queue_id": queue_id, "item_ids": [row_id, other_id], "cancelled_item_ids": [row_id]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["retried"] == [other_id]
    assert body["left_in_place"] == [{"id": row_id, "reason": "plate_answer_required"}]
    db_session.expire_all()
    assert (await db_session.get(PrintQueueItem, row_id)).status == "cancelled"
    assert (await db_session.get(PrintQueueItem, other_id)).status == "pending"
    assert await gate_of(db_session, pid) == (True, aid)


@pytest.mark.asyncio
async def test_retry_of_a_history_row_is_allowed_while_the_gate_waits_on_another(
    async_client, printer_factory, db_session
):
    pid, aid, row_id = await owned_gate(db_session, printer_factory)
    row = await db_session.get(PrintQueueItem, row_id)
    old = PrintArchive(printer_id=pid, filename="old.3mf", file_path="old.3mf", file_size=1, status="cancelled")
    db_session.add(old)
    await db_session.flush()
    history = PrintQueueItem(queue_id=row.queue_id, archive_id=old.id, status="cancelled", library_file_id=1)
    db_session.add(history)
    await db_session.commit()
    history_id = history.id

    response = await async_client.post(f"/api/v1/queue/{history_id}/retry")

    assert response.status_code == 200, response.text
    db_session.expire_all()
    assert (await db_session.get(PrintQueueItem, history_id)).status == "pending"
    assert await gate_of(db_session, pid) == (True, aid)


@pytest.mark.asyncio
async def test_retry_after_the_plate_was_cleared_is_allowed(async_client, printer_factory, db_session):
    pid, aid, row_id = await owned_gate(db_session, printer_factory)
    with _finished_printer():
        cleared = await async_client.post(f"/api/v1/printers/{pid}/clear-plate")
    assert cleared.status_code == 200, cleared.text
    assert await gate_of(db_session, pid) == (False, None)

    response = await async_client.post(f"/api/v1/queue/{row_id}/retry")

    assert response.status_code == 200, response.text
    db_session.expire_all()
    assert (await db_session.get(PrintQueueItem, row_id)).status == "pending"


@pytest.mark.asyncio
async def test_the_queue_says_which_row_the_plate_answer_belongs_to(async_client, printer_factory, db_session):
    pid, aid, row_id = await owned_gate(db_session, printer_factory)
    row = await db_session.get(PrintQueueItem, row_id)
    other = PrintQueueItem(queue_id=row.queue_id, status="failed", position=5, library_file_id=1)
    db_session.add(other)
    await db_session.commit()
    queue_id, other_id = row.queue_id, other.id

    listed = await async_client.get("/api/v1/queue/", params={"queue_id": queue_id})
    issues = await async_client.get("/api/v1/queue/issues", params={"queue_id": queue_id})

    for response, rows in ((listed, listed.json()), (issues, issues.json()["items"])):
        assert response.status_code == 200, response.text
        flags = {entry["id"]: entry["plate_answer_required"] for entry in rows if entry.get("id") in (row_id, other_id)}
        assert flags == {row_id: True, other_id: False}

    with _finished_printer():
        assert (await async_client.post(f"/api/v1/printers/{pid}/clear-plate")).status_code == 200
    listed = await async_client.get("/api/v1/queue/", params={"queue_id": queue_id})
    assert {entry["id"]: entry["plate_answer_required"] for entry in listed.json()}[row_id] is False


@pytest.mark.asyncio
async def test_repeat_on_the_card_still_rearms_the_owner_and_releases_the_gate(
    async_client, printer_factory, db_session
):
    pid, _aid, row_id = await owned_gate(db_session, printer_factory)

    with _finished_printer():
        response = await async_client.post(f"/api/v1/printers/{pid}/repeat-print")

    assert response.status_code == 200, response.text
    db_session.expire_all()
    assert (await db_session.get(PrintQueueItem, row_id)).status == "pending"
    assert await gate_of(db_session, pid) == (False, None)
