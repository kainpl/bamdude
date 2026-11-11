"""Print state diagnostics restore a row an earlier Retry put back in the queue (spec plate-gate-owner-requeue B).

The incident's shape: an aborted print left its row cancelled and the plate gate armed on its archive; a
Retry made the row pending again and every plate answer refused. The repair returns the row to its run's
outcome and touches nothing else; the operator then answers on the card (T7-T10).
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.print_state_recovery_action import PrintStateRecoveryAction
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import print_state_recovery_preview as tokens, print_state_recovery_store as store
from backend.app.services.print_state_recovery import GATE_OWNER_ACTION
from backend.tests.integration.test_plate_answers_defects import _finished_printer
from backend.tests.unit.services.test_terminal_queue_repair import telemetry  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def requeued_owner(db, printer_factory, archive_factory, *, outcome="cancelled"):
    printer = await printer_factory()
    queue = PrinterQueue(id=printer.id, printer_id=printer.id, status="paused", is_paused=True)
    db.add(queue)
    await db.flush()
    claim = datetime.now(timezone.utc) - timedelta(hours=2)
    archive = await archive_factory(
        printer.id,
        queue_id=queue.id,
        status=outcome,
        started_at=claim + timedelta(seconds=1),
        completed_at=claim + timedelta(minutes=45),
        failure_reason="userCancelled" if outcome == "cancelled" else None,
    )
    # What an older Retry left: pending again, still naming the archive, no dispatch since.
    row = PrintQueueItem(
        queue_id=queue.id,
        archive_id=archive.id,
        status="pending",
        position=1,
        started_at=claim,
        dispatch_attempts=0,
        library_file_id=1,
    )
    db.add(row)
    await db.flush()
    printer.awaiting_plate_clear = True
    printer.awaiting_plate_clear_archive_id = archive.id
    printer.awaiting_plate_clear_token = "gate-token"
    queue.pending_count = 1
    await db.commit()
    return printer, queue, archive, row


def factory(test_engine):
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


async def test_the_page_names_the_requeued_row_and_offers_the_restore(
    db_session, printer_factory, archive_factory, telemetry
):
    printer, _queue, archive, row = await requeued_owner(db_session, printer_factory, archive_factory)

    page = await store.list_issues(db_session, None)

    issue = next(entry for entry in page["issues"] if entry["kind"] == "item" and entry["target_id"] == row.id)
    assert issue["code"] == "plate_gate_owner_requeued"
    assert issue["actions"] == [GATE_OWNER_ACTION]
    assert (issue["printer_id"], issue["archive_id"]) == (printer.id, archive.id)
    assert sum(entry["target_id"] == row.id for entry in page["issues"]) == 1


@pytest.mark.parametrize("answer", ["clear-plate", "repeat-print"])
async def test_restore_then_the_card_answers_again(
    db_session, test_engine, async_client, printer_factory, archive_factory, telemetry, answer
):
    printer, queue, archive, row = await requeued_owner(db_session, printer_factory, archive_factory)
    pid, aid, row_id, qid, completed = printer.id, archive.id, row.id, queue.id, archive.completed_at

    preview = await store.make_preview(db_session, pid, "item", row_id, GATE_OWNER_ACTION, "user:1")
    payload = tokens.verify(preview["preview_token"])
    result = await store.apply_preview(factory(test_engine), payload, "user:1", "Operator")
    repeated = await store.apply_preview(factory(test_engine), payload, "user:1", "Operator")

    assert repeated == result  # T9: the same confirmation answers its first result
    db_session.expire_all()
    restored = await db_session.get(PrintQueueItem, row_id)
    assert restored.status == "cancelled"
    assert restored.completed_at.replace(tzinfo=timezone.utc) == completed.replace(tzinfo=timezone.utc)
    gate = await db_session.get(Printer, pid)
    assert (gate.awaiting_plate_clear, gate.awaiting_plate_clear_archive_id) == (True, aid)  # the gate is untouched
    q = await db_session.get(PrinterQueue, qid)
    assert q.is_paused and q.pending_count == 0
    assert await db_session.scalar(select(func.count()).select_from(PrintStateRecoveryAction)) == 1

    with _finished_printer():
        response = await async_client.post(f"/api/v1/printers/{pid}/{answer}")
    assert response.status_code == 200, response.text
    db_session.expire_all()
    assert not (await db_session.get(Printer, pid)).awaiting_plate_clear
    if answer == "repeat-print":
        assert (await db_session.get(PrintQueueItem, row_id)).status == "pending"


@pytest.mark.parametrize("change", ["gate_answered", "gate_rearmed", "row_cancelled", "row_dispatched"])
async def test_a_stale_preview_changes_nothing(
    db_session, test_engine, printer_factory, archive_factory, telemetry, change
):
    printer, _queue, _archive, row = await requeued_owner(db_session, printer_factory, archive_factory)
    pid, row_id = printer.id, row.id
    preview = await store.make_preview(db_session, pid, "item", row_id, GATE_OWNER_ACTION, "user:1")
    if change == "gate_answered":
        printer.awaiting_plate_clear = False
        printer.awaiting_plate_clear_archive_id = None
    elif change == "gate_rearmed":
        printer.awaiting_plate_clear_token = "another-gate"
    elif change == "row_cancelled":
        row.status = "cancelled"
    else:
        row.dispatch_attempts = 1
    await db_session.commit()
    expected_status = row.status

    with pytest.raises(ValueError, match="recovery_changed"):
        await store.apply_preview(factory(test_engine), tokens.verify(preview["preview_token"]), "user:1", "Operator")

    db_session.expire_all()
    assert (await db_session.get(PrintQueueItem, row_id)).status == expected_status
    assert await db_session.scalar(select(func.count()).select_from(PrintStateRecoveryAction)) == 0


async def test_restore_takes_the_same_rights_as_aligning_an_item():
    from backend.app.api.routes.print_state_recovery import WRITE_PERMISSIONS
    from backend.app.core.permissions import Permission

    assert WRITE_PERMISSIONS[GATE_OWNER_ACTION] == (Permission.QUEUE_UPDATE_ALL, Permission.PRINTERS_CONTROL)
    assert WRITE_PERMISSIONS[GATE_OWNER_ACTION] == WRITE_PERMISSIONS["align_terminal_item"]
