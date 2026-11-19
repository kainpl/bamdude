"""The one reader of the ``require_previous_success`` gate (m116), and its writers.

"Only after a successful previous print" asks one question of a printer: did the
newest finished print on it end well? It is answered here, for both queue tiers
(``PrintScheduler.check_queue``, ``auto_queue_eligibility.find_eligible_printer``,
the rebalancer through the scheduler), and nowhere else.

The lookback reads the printer's queue rows in ``completed`` / ``failed`` /
``cancelled``, newest first, leaving out the rows marked ``gate_acknowledged``:

* ``cancelled`` is **neutral** — a person stopping a print is a decision, not a
  failure (upstream #1667).
* ``skipped`` is out of the lookback — a skip was never a print attempt.
* Only ``failed`` closes the gate.

⚠️ **The order states its NULL rule and ends with the id** (upstream 5c575b5ba):
``completed_at DESC NULLS LAST, id DESC``. A bare ``DESC`` sorts NULL first on
PostgreSQL, so one cancel that left no end time used to hide every later failure
there, while SQLite sorted it last.

⚠️ **A success releases the gate by itself, durably** (owner, G03 D1(a)). A
successful queue row is deleted at completion or at Clear plate; failed rows
stay. So a completion that finishes its row as ``completed`` marks every older
unacknowledged failure of that queue acknowledged in the same transaction
(:func:`acknowledge_failures_before_success`) — otherwise the next row to read
would be the old failure again, and one unretried failure would keep the gate
shut for ever.

⚠️ **Nothing that was not sent to the printer trips it** (owner, G03 O-3 (b)): a
failed attempt that published no start is written ``gate_acknowledged``; a
"did not start" verdict after a published start still gates. Those rows are
written where the attempt ends (``print_scheduler._fail_attempt`` /
``_fail_item``, ``background_dispatch._release_direct_claim``), not here.

No finished print at all means nothing to gate on, so it passes: a gate must
never block the first job on a fresh printer.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue

#: The rows the lookback reads. ``skipped`` is deliberately absent.
LOOKBACK_STATUSES = ("completed", "failed", "cancelled")


def _newest_first():
    """The lookback's order: the end time with NULL last, then the id — "the newest" is always defined."""
    return (PrintQueueItem.completed_at.desc().nullslast(), PrintQueueItem.id.desc())


def _unacknowledged_terminal():
    return (
        PrintQueueItem.status.in_(LOOKBACK_STATUSES),
        PrintQueueItem.gate_acknowledged.is_(False),
    )


async def previous_print_succeeded(db: AsyncSession, printer_id: int, *, exclude_item_id: int | None = None) -> bool:
    """Did the newest finished print on this printer end well (or is there none)?

    ``exclude_item_id`` keeps a row from gating on itself.
    """
    stmt = (
        select(PrintQueueItem.status)
        .join(PrinterQueue, PrintQueueItem.queue_id == PrinterQueue.id)
        .where(PrinterQueue.printer_id == printer_id, *_unacknowledged_terminal())
        .order_by(*_newest_first())
        .limit(1)
    )
    if exclude_item_id is not None:
        stmt = stmt.where(PrintQueueItem.id != exclude_item_id)
    previous = await db.scalar(stmt)
    return previous is None or previous != "failed"


async def failure_blocks(db: AsyncSession, printer_id: int) -> bool:
    """Whether the gate would refuse gated work on this printer now — the printer card's flag."""
    return not await previous_print_succeeded(db, printer_id)


async def blocking_failure_queue_ids(db: AsyncSession, queue_ids: Iterable[int]) -> set[int]:
    """:func:`failure_blocks` for many queues in ONE query: the queues whose newest finished row is a failure.

    By queue id — a printer's queue carries the printer's own id (``ensure_printer_queue``), so this is the same
    question as :func:`failure_blocks` asked per printer.
    """
    ids = sorted(set(queue_ids))
    if not ids:
        return set()
    ranked = (
        select(
            PrintQueueItem.queue_id.label("queue_id"),
            PrintQueueItem.status.label("status"),
            func.row_number().over(partition_by=PrintQueueItem.queue_id, order_by=_newest_first()).label("rank"),
        )
        .where(PrintQueueItem.queue_id.in_(ids), *_unacknowledged_terminal())
        .subquery()
    )
    rows = await db.scalars(select(ranked.c.queue_id).where(ranked.c.rank == 1, ranked.c.status == "failed"))
    return set(rows)


async def acknowledge_newest_failure(db: AsyncSession, queue_id: int) -> bool:
    """Retire the failure the gate is reading on this queue; whether there was one.

    The same lookback and order as :func:`previous_print_succeeded`: it marks the
    newest finished row only when that row is the failure — if a print has
    succeeded since, nothing is gating and nothing is acknowledged. Never commits;
    the caller holds the queue's lock and commits.
    """
    blocking = await db.scalar(
        select(PrintQueueItem)
        .where(PrintQueueItem.queue_id == queue_id, *_unacknowledged_terminal())
        .order_by(*_newest_first())
        .limit(1)
    )
    if blocking is None or blocking.status != "failed":
        return False
    blocking.gate_acknowledged = True
    return True


async def acknowledge_failures_before_success(db: AsyncSession, queue_id: int, *, success_item_id: int) -> int:
    """A print on this queue just finished well: every older failure stops gating. Returns how many were marked.

    Called by every writer that finishes a queue row as ``completed`` (the live completion, the
    reconciliation's close, the structural recoveries), in that writer's own transaction, before it commits —
    the success row itself is deleted soon after (completion clean-up, Clear plate, leftovers), and only this
    mark keeps "a later success released the gate" true once it is gone.

    "Older" needs no time comparison: while this row was printing it held the printer's claim, so every
    failure of the queue had already ended. The rows stay ``failed`` — the history is untouched; only their
    weight in the gate goes. A Retry re-arms a row and its claim clears the mark again. Never commits.
    """
    result = await db.execute(
        update(PrintQueueItem)
        .where(
            PrintQueueItem.queue_id == queue_id,
            PrintQueueItem.status == "failed",
            PrintQueueItem.gate_acknowledged.is_(False),
            PrintQueueItem.id != success_item_id,
        )
        .values(gate_acknowledged=True)
        .execution_options(synchronize_session="fetch")
    )
    return result.rowcount or 0
