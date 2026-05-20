"""Regression test: a runtime cancel/abort pauses the per-printer queue.

The MQTT ``on_print_complete`` callback delivers ``status="aborted"``
(normalised to ``"cancelled"``) for both user-initiated cancels (Stop
button on the printer screen or in the BamDude UI) **and** printer-
initiated aborts on mid-print errors (auto-bed-leveling failure,
runaway thermistor, AMS jam, …) — gcode_state goes RUNNING → IDLE
without ever flipping to FAILED, so the runtime can't distinguish the
two locally.

Pre-fix: ``main.py::on_print_complete`` routed every cancelled item to
``set_queue_idle``, leaving ``queue.status='idle'``. The scheduler's
next tick then happily dispatched the next pending queue item — the
operator could just barely fix the bed-leveling cause before the
printer kicked off the second print regardless. Reported by the
operator's queue-of-2 scenario.

Post-fix: cancelled flips the queue to ``paused`` so the scheduler's
``queue.status in ("paused", "error")`` skip-guard at
``print_scheduler.py:227`` blocks the next dispatch. Operator resumes
via the explicit UI control once the cause is cleared.

Pinned via the ``set_queue_paused`` helper directly — exercises the
same callsite invariant that ``main.py`` now uses, without spinning up
the MQTT layer.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.queue_counters import (
    set_queue_idle,
    set_queue_paused,
)


@pytest.mark.asyncio
async def test_runtime_cancel_pauses_queue_not_idle(db_session):
    """The ``cancelled`` branch in ``on_print_complete`` flips the queue
    to ``paused`` so the scheduler stops dispatching. ``idle`` was the
    pre-fix value and would let the next pending item start without
    operator intervention."""
    db_session.add(
        Printer(
            id=1,
            name="p",
            serial_number="TEST-CANCEL-1",
            ip_address="127.0.0.1",
            access_code="00000000",
        )
    )
    db_session.add(PrinterQueue(id=1, printer_id=1, status="printing", current_item_id=10))
    db_session.add(
        PrintQueueItem(
            id=10,
            queue_id=1,
            status="cancelled",
            completed_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    await set_queue_paused(db_session, 1, paused_item_id=10)
    await db_session.commit()

    queue = (await db_session.execute(select(PrinterQueue).where(PrinterQueue.id == 1))).scalar_one()
    assert queue.status == "paused"
    # ``current_item_id`` cleared so the scheduler's "skip items whose
    # queue is paused" branch can't accidentally find the just-cancelled
    # item still pinned as the active one.
    assert queue.current_item_id is None


@pytest.mark.asyncio
async def test_completed_still_goes_idle(db_session):
    """Sanity guard — the happy path is unchanged: a clean ``completed``
    print still flips the queue to ``idle``, NOT paused. Otherwise every
    successful print would block the rest of the queue from dispatching."""
    db_session.add(
        Printer(
            id=2,
            name="p2",
            serial_number="TEST-CANCEL-2",
            ip_address="127.0.0.1",
            access_code="00000000",
        )
    )
    db_session.add(PrinterQueue(id=2, printer_id=2, status="printing", current_item_id=20))
    await db_session.commit()

    await set_queue_idle(db_session, 2)
    await db_session.commit()

    queue = (await db_session.execute(select(PrinterQueue).where(PrinterQueue.id == 2))).scalar_one()
    assert queue.status == "idle"
