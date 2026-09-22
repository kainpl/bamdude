"""Shared facts and final guards for one printer's queue lane.

Routing may use a cheap snapshot, but a writer must ask these facts again while
it owns the queue lane.  This module deliberately says nothing about MQTT
readiness, filament routing or HTTP: those are policy at each caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue


class PrinterOccupancyConflict(RuntimeError):
    """An expected final-admission refusal, safe to retry on another printer."""

    def __init__(self, code: str, *, queue_id: int):
        self.code = code
        self.queue_id = queue_id
        super().__init__(f"printer queue {queue_id}: {code}")


@dataclass(frozen=True)
class QueueOccupancy:
    queue: PrinterQueue
    printing_item_ids: tuple[int, ...]
    pending_item_ids: tuple[int, ...]

    @property
    def has_active_claim(self) -> bool:
        return self.queue.status == "printing" or bool(self.printing_item_ids)


def claim_token_matches(actual: datetime | None, expected: datetime | None) -> bool:
    """Compare a claim token across SQLite's timezone-less storage boundary."""
    if actual is None or expected is None:
        return False
    if actual == expected:
        return True
    actual_utc = actual if actual.tzinfo is not None else actual.replace(tzinfo=timezone.utc)
    expected_utc = expected if expected.tzinfo is not None else expected.replace(tzinfo=timezone.utc)
    return actual_utc == expected_utc


async def read_queue_occupancy(db: AsyncSession, queue_id: int, *, for_update: bool = False) -> QueueOccupancy:
    """Read durable lane facts; caller already owns ``queue_claim_scope``."""
    statement = select(PrinterQueue).where(PrinterQueue.id == queue_id).execution_options(populate_existing=True)
    if for_update:
        statement = statement.with_for_update()
    queue = (await db.execute(statement)).scalar_one_or_none()
    if queue is None:
        raise PrinterOccupancyConflict("queue_missing", queue_id=queue_id)
    rows = (
        await db.execute(
            select(PrintQueueItem.id, PrintQueueItem.status).where(
                PrintQueueItem.queue_id == queue_id,
                PrintQueueItem.status.in_(("pending", "printing")),
            )
        )
    ).all()
    return QueueOccupancy(
        queue=queue,
        printing_item_ids=tuple(item_id for item_id, status in rows if status == "printing"),
        pending_item_ids=tuple(item_id for item_id, status in rows if status == "pending"),
    )


async def active_claim_printer_ids(db: AsyncSession) -> set[int]:
    """Printer ids with either half of a durable active claim."""
    headers = await db.execute(select(PrinterQueue.printer_id).where(PrinterQueue.status == "printing"))
    children = await db.execute(
        select(PrinterQueue.printer_id)
        .join(PrintQueueItem, PrintQueueItem.queue_id == PrinterQueue.id)
        .where(PrintQueueItem.status == "printing")
        .distinct()
    )
    return {printer_id for (printer_id,) in headers.all()} | {printer_id for (printer_id,) in children.all()}


def require_direct_admission(occupancy: QueueOccupancy) -> None:
    """A new direct command may coexist with pending work, never a live claim."""
    queue = occupancy.queue
    if queue.is_paused or queue.status in {"paused", "error"}:
        raise PrinterOccupancyConflict("queue_paused", queue_id=queue.id)
    if occupancy.has_active_claim:
        raise PrinterOccupancyConflict("active_claim", queue_id=queue.id)


def require_auto_placement(occupancy: QueueOccupancy) -> None:
    """Auto queue needs an empty lane; pending work is its own backlog fact."""
    queue = occupancy.queue
    if queue.is_paused:
        raise PrinterOccupancyConflict("queue_paused", queue_id=queue.id)
    if occupancy.has_active_claim:
        raise PrinterOccupancyConflict("active_claim", queue_id=queue.id)
    if occupancy.pending_item_ids:
        raise PrinterOccupancyConflict("pending_backlog", queue_id=queue.id)


def require_scheduler_claim(occupancy: QueueOccupancy) -> None:
    """Permit the scheduler's own pending item, reject any other live owner."""
    queue = occupancy.queue
    if queue.is_paused or queue.status in {"paused", "error"}:
        raise PrinterOccupancyConflict("queue_paused", queue_id=queue.id)
    # This check happens immediately before pending→printing.  The candidate is
    # therefore not a printing owner yet; either half of an existing claim is
    # another run (including a damaged header-only state).
    if occupancy.has_active_claim:
        raise PrinterOccupancyConflict("active_claim", queue_id=queue.id)
