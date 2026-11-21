"""An edit of a queued job validates outside any lane and is applied under the lanes (G03 B, A6).

Validation (in the request session, no lane): ownership, the status pre-check, model / plate checks, the swap
decision, the routing intent — which reads the job's bytes — and JSON serialisation. Its result is an
:class:`EditPlan` per row: the column values, the basis and the queue the row was judged in, and the facts of the
target printer the checks used — read by :func:`checked_facts` BEFORE the first check, so every check reads the
header and printer rows that read refreshed, never a later state (a header deleted or replaced while the checks
ran is not the one they judged).

Apply (a fresh session under ``claim_lanes(sorted({every row's queue} ∪ {target queue}))``): the rows re-read
``FOR NO KEY UPDATE`` in ascending id, addressed by the queue each was judged in — a row that has since left it is
never locked, its lane is not held (L1); each must still be pending, with the basis the validation read, and the
target printer's facts must be the ones the checks used; a move whose target header is missing at either read is
refused. Then the values are written, counters recounted for every queue whose rows changed (ascending), one
commit. A row that changed is refused — the single and batch edits refuse whole (nothing written), the bulk edit
skips that row.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from sqlalchemy import select, tuple_

from backend.app.core import database
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.queue_row_basis import row_basis


async def printer_facts(db, queue_id: int | None) -> tuple | None:
    """What an edit's checks used about the printer it lands on: the queue, its printer, the model, whether the
    printer is archived, and its swap mode."""
    if queue_id is None:
        return None
    queue = await db.get(PrinterQueue, queue_id, populate_existing=True)
    if queue is None:
        return None
    printer = await db.get(Printer, queue.printer_id, populate_existing=True) if queue.printer_id else None
    return (
        queue.id,
        queue.recovery_identity,  # a move lands only in the header the validation read (A25)
        queue.printer_id,
        getattr(printer, "model", None),
        bool(getattr(printer, "archived", False)),
        bool(getattr(printer, "swap_mode_enabled", False)),
    )


@dataclass(frozen=True)
class EditPlan:
    item_id: int
    queue_id: int
    basis: str
    target_queue_id: int
    facts: tuple | None
    values: dict = field(default_factory=dict)


async def checked_facts(db, queue_ids: Iterable[int | None]) -> dict[int, tuple | None]:
    """The facts of every queue an edit lands in, read once BEFORE the edit's checks (A6, A25).

    ``printer_facts`` loads the header and printer rows with ``populate_existing``, so the checks that follow —
    which reach them through ``db.get`` — read exactly the rows these facts describe; the plan records these
    facts, never a read taken after the checks.
    """
    return {queue_id: await printer_facts(db, queue_id) for queue_id in sorted({q for q in queue_ids if q is not None})}


def plan_edit(
    item: PrintQueueItem, values: dict, *, basis: str, target_queue_id: int | None, facts: dict[int, tuple | None]
) -> EditPlan:
    """One row's validated edit; ``facts`` is :func:`checked_facts` of the queues the checks judged."""
    target = target_queue_id if target_queue_id is not None else item.queue_id
    return EditPlan(item.id, item.queue_id, basis, target, facts.get(target), dict(values))


@dataclass(frozen=True)
class ApplyResult:
    applied: list[int]
    changed: list[int]


async def apply_edits(
    plans: Iterable[EditPlan],
    *,
    set_field: Callable[[PrintQueueItem, str, object], None],
    all_or_nothing: bool,
) -> ApplyResult:
    """Apply validated edits under their lanes; ``set_field(row, name, value)`` writes one value."""
    from backend.app.services.queue_counters import update_queue_counters
    from backend.app.services.queue_ops import claim_lanes

    plans = sorted(plans, key=lambda plan: plan.item_id)
    if not plans:
        return ApplyResult([], [])
    lanes = sorted({plan.queue_id for plan in plans} | {plan.target_queue_id for plan in plans})
    async with database.async_session() as db, claim_lanes(db, lanes):
        rows = {
            row.id: row
            for row in await db.scalars(
                select(PrintQueueItem)
                # Each row only in the queue it was judged in, whose lane is held (L1): one that left it is not
                # locked, and its absence here is the refusal.
                .where(tuple_(PrintQueueItem.id, PrintQueueItem.queue_id).in_([(p.item_id, p.queue_id) for p in plans]))
                .order_by(PrintQueueItem.id)
                .with_for_update(key_share=True)
                .execution_options(populate_existing=True)
            )
        }
        facts = {queue_id: await printer_facts(db, queue_id) for queue_id in sorted({p.target_queue_id for p in plans})}
        applied: list[int] = []
        changed: list[int] = []
        touched: set[int] = set()
        for plan in plans:
            row = rows.get(plan.item_id)
            moves = plan.target_queue_id != plan.queue_id
            if (
                row is None
                or row.status != "pending"
                or row.queue_id != plan.queue_id
                or row_basis(row) != plan.basis
                or facts.get(plan.target_queue_id) != plan.facts
                # A move lands only in a header that existed when it was checked and still does (A25): two
                # absences are no match.
                or (moves and plan.facts is None)
            ):
                changed.append(plan.item_id)
                continue
            for name, value in plan.values.items():
                set_field(row, name, value)
            touched |= {plan.queue_id, row.queue_id}
            applied.append(plan.item_id)
        if all_or_nothing and changed:
            await db.rollback()
            return ApplyResult([], changed)
        for queue_id in sorted(touched):
            await update_queue_counters(db, queue_id)
        await db.commit()
    return ApplyResult(applied, changed)
