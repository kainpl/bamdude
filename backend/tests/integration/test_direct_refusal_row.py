"""A refused direct print leaves an honest row: failed, why, and a Retry.

Spec direct-print-silent-cancel §4.1. The row used to become ``cancelled`` —
which reads as the operator's own act — for a print that BamDude refused to
start and will never start on its own (no future autostart).
"""

from datetime import datetime

import pytest

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.filament_deferred import defer_claim
from backend.app.services.filament_intake import routing_detail

pytestmark = pytest.mark.integration


async def test_a_direct_refusal_leaves_a_failed_row_the_operator_can_retry(
    committing_client, db_session, printer_factory
):
    printer = await printer_factory()
    queue = PrinterQueue(id=printer.id, printer_id=printer.id, status="printing")
    db_session.add(queue)
    await db_session.flush()
    started = datetime(2026, 9, 24, 22, 22)
    item = PrintQueueItem(queue_id=queue.id, status="printing", started_at=started, origin="direct")
    db_session.add(item)
    await db_session.flush()
    queue.current_item_id = item.id
    await db_session.commit()

    assert await defer_claim(db_session, item_id=item.id, started_at=started, reason="feed_settle_timeout", direct=True)
    await db_session.commit()
    await db_session.refresh(item)
    await db_session.refresh(queue)

    assert item.status == "failed"
    assert item.error_message == routing_detail("feed_settle_timeout")["message"]
    assert item.waiting_reason is None
    assert item.gate_acknowledged is True, "a print never sent is no physical failure for require_previous_success"
    assert item.completed_at is not None
    assert (queue.status, queue.current_item_id) == ("idle", None)

    retried = await committing_client.post(f"/api/v1/queue/{item.id}/retry")
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "pending"
