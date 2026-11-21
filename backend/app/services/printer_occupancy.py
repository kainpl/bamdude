"""Shared facts and final guards for one printer's queue lane.

Routing may use a cheap snapshot, but a writer must ask these facts again while
it owns the queue lane.  This module deliberately says nothing about MQTT
readiness, filament routing or HTTP: those are policy at each caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import Row, and_, or_, select
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
    pending_rows: tuple[Row[tuple[int, str, bool, datetime | None]], ...]

    @property
    def pending_item_ids(self) -> tuple[int, ...]:
        return tuple(row.id for row in self.pending_rows)

    @property
    def holding_item_ids(self) -> tuple[int, ...]:
        # Read the clock at placement admission, while the lane still owns
        # these rows. A dispatch claim takes its own instant at the head query.
        now = datetime.now(timezone.utc)
        return tuple(row.id for row in self.pending_rows if row_holds_printer(row, now))

    @property
    def has_active_claim(self) -> bool:
        return self.queue.status == "printing" or bool(self.printing_item_ids)


def holding_condition(now: datetime):
    """A pending row holds the lane once the scheduler could start it on its own."""
    return and_(
        PrintQueueItem.status == "pending",
        PrintQueueItem.manual_start.is_(False),
        or_(PrintQueueItem.scheduled_time.is_(None), PrintQueueItem.scheduled_time <= now),
    )


def scheduled_for_later(scheduled: datetime | None, now: datetime) -> bool:
    """The database stores scheduled times in UTC, sometimes without tzinfo."""
    if scheduled is None:
        return False
    scheduled = scheduled.replace(tzinfo=timezone.utc) if scheduled.tzinfo is None else scheduled
    now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
    return scheduled > now


def row_holds_printer(row: PrintQueueItem | Row[tuple[int, str, bool, datetime | None]], now: datetime) -> bool:
    return row.status == "pending" and row.manual_start is False and not scheduled_for_later(row.scheduled_time, now)


def placement_rank(ready: bool, exact: bool, colours: int, has_deferred_rows: bool, printer_id: int):
    return ready, exact, colours, not has_deferred_rows, -printer_id


def claim_token_matches(actual: datetime | None, expected: datetime | None) -> bool:
    """Compare a claim token across SQLite's timezone-less storage boundary."""
    if actual is None or expected is None:
        return False
    if actual == expected:
        return True
    actual_utc = actual if actual.tzinfo is not None else actual.replace(tzinfo=timezone.utc)
    expected_utc = expected if expected.tzinfo is not None else expected.replace(tzinfo=timezone.utc)
    return actual_utc == expected_utc


async def queue_header_holds(db: AsyncSession, queue_id: int, recovery_identity: str | None) -> bool:
    """A row enters queue Q only while Q's header is the one its validation read (G03 B, A25).

    Read again under Q's lane, after the lane's guards and before the first insert or move, ``FOR NO KEY UPDATE``:
    missing, or another incarnation -- SQLite gives a deleted printer's id, and its header's, to the next printer --
    is the door's own answer for a missing queue. ``recovery_identity`` is what the validation read."""
    row = (
        await db.execute(
            select(PrinterQueue.id, PrinterQueue.recovery_identity)
            .where(PrinterQueue.id == queue_id)
            .with_for_update(key_share=True)
        )
    ).first()
    return row is not None and row.recovery_identity == recovery_identity


async def queue_header_identity(queue_id: int) -> str | None:
    """The incarnation of header Q, read in a short session of its own -- what a door that resolves its queue by id
    validates against (A25)."""
    from backend.app.core.database import async_session

    async with async_session() as db:
        return await db.scalar(select(PrinterQueue.recovery_identity).where(PrinterQueue.id == queue_id))


async def read_queue_occupancy(db: AsyncSession, queue_id: int, *, for_update: bool = False) -> QueueOccupancy:
    """Read durable lane facts; caller already owns ``queue_claim_scope``."""
    statement = select(PrinterQueue).where(PrinterQueue.id == queue_id).execution_options(populate_existing=True)
    if for_update:
        statement = statement.with_for_update(key_share=True)
    queue = (await db.execute(statement)).scalar_one_or_none()
    if queue is None:
        raise PrinterOccupancyConflict("queue_missing", queue_id=queue_id)
    rows = (
        await db.execute(
            select(
                PrintQueueItem.id,
                PrintQueueItem.status,
                PrintQueueItem.manual_start,
                PrintQueueItem.scheduled_time,
            ).where(
                PrintQueueItem.queue_id == queue_id,
                PrintQueueItem.status.in_(("pending", "printing")),
            )
        )
    ).all()
    return QueueOccupancy(
        queue=queue,
        printing_item_ids=tuple(row.id for row in rows if row.status == "printing"),
        pending_rows=tuple(row for row in rows if row.status == "pending"),
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
    """AutoQueue needs a lane with no claim or due, unstaged backlog.

    A queue halted after a failed or cancelled print (``status`` ``paused`` / ``error``) refuses new work as an
    operator-paused one does (owner, G03 E-3) — the same rule as the direct and the scheduler admissions, read
    again here under the lane's lock because routing chose the printer from an earlier read.
    """
    queue = occupancy.queue
    if queue.is_paused or queue.status in {"paused", "error"}:
        raise PrinterOccupancyConflict("queue_paused", queue_id=queue.id)
    if occupancy.has_active_claim:
        raise PrinterOccupancyConflict("active_claim", queue_id=queue.id)
    if occupancy.holding_item_ids:
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
