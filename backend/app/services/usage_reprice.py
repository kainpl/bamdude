"""Re-price recorded filament usage — an explicit, scoped correction (owner's Р4, upstream 4f70d3da6 adapted).

A print's cost is a SNAPSHOT: every usage row carries what its grams cost at the
spool's price when the print was booked, and the archive's cost is the sum of
its rows plus the default-rate share for grams no spool covered. Nothing
rewrites that by itself — a spool record re-priced for a new purchase must not
move what last spring's prints cost, nor the cost of an order that shipped.

The correction case is the exception: a price typed wrong (200 /kg for 20)
costs every print from that spool tenfold, and re-summing stored rows cannot
fix it. This module rewrites the cost of the usage rows of ONE spool and / or
ONE period at the spool's current price, and moves each archive by exactly the
difference of its rows — so another spool's share, and the default-rate share,
stay as they were recorded.

⚠️ It is the only writer of ``SpoolUsageHistory.cost`` outside the usage
tracker, and it uses the tracker's rate rule (``filament_cost.usage_cost``).
It never touches grams — ``weight_used`` on a row, on a spool or on an archive
— and never energy, which is settled with its own provenance.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.database import take_write_lock
from backend.app.models.archive import PrintArchive
from backend.app.models.spool import Spool
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services.filament_cost import default_rate_per_kg, usage_cost

logger = logging.getLogger(__name__)


class ScopeRequired(ValueError):
    """Re-pricing was asked for everything: it takes a spool, a period, or both."""


@dataclass
class RepriceResult:
    dry_run: bool
    # Usage rows in the selection, and how many of them cost something else now.
    rows: int = 0
    rows_changed: int = 0
    # Archives whose cost moves, and the orders those archives are filed under.
    archives_changed: int = 0
    orders_changed: int = 0
    # The selection's rows, summed: as recorded and at the current prices.
    cost_before: float = 0.0
    cost_after: float = 0.0

    def as_dict(self) -> dict:
        return asdict(self)


def _selection(spool_id: int | None, start: datetime | None, end: datetime | None):
    """The rows in scope with their spool's CURRENT price, oldest first.

    The join is what keeps a row whose spool is gone out of it: there is no
    price to re-price it at, so it keeps the cost it was booked with.
    """
    query = (
        select(SpoolUsageHistory, Spool.cost_per_kg)
        .join(Spool, Spool.id == SpoolUsageHistory.spool_id)
        # Print rows only. A lifetime correction (a weight sync, a runout
        # zero) is the spool's own ledger: no archive's cost was built from it.
        .where(SpoolUsageHistory.print_contribution_filter())
    )
    if spool_id is not None:
        query = query.where(SpoolUsageHistory.spool_id == spool_id)
    if start is not None:
        query = query.where(SpoolUsageHistory.created_at >= start)
    if end is not None:
        query = query.where(SpoolUsageHistory.created_at < end)
    return query.order_by(SpoolUsageHistory.id)


def _naive(value: datetime | None) -> datetime | None:
    """Stored timestamps are naive UTC; compare like with like on every backend."""
    return value.replace(tzinfo=None) if value is not None and value.tzinfo is not None else value


def _booked_at_this_price(grams: float | None, booked: float, spool_price, default_rate: float) -> bool:
    """Whether ``booked`` is what the row's grams cost at today's price already.

    A row keeps its weight to 0.1 g, but its cost was booked from the exact
    grams: 2.16 g at 25 /kg is 0.05, and the stored 2.2 g prices at 0.06. Any
    cost the weights that round to this row's could have had is "this price" —
    without it a re-price moved rows by a cent when no price had changed.
    """
    if grams is None:
        return False
    low = usage_cost(max(grams - 0.05, 0.0), spool_price, default_rate) or 0.0
    high = usage_cost(grams + 0.05, spool_price, default_rate) or 0.0
    return round(low, 2) <= round(booked, 2) <= round(high, 2)


async def reprice(
    db: AsyncSession,
    *,
    spool_id: int | None,
    start: datetime | None,
    end: datetime | None,
    apply: bool,
) -> RepriceResult:
    """Preview (``apply=False``) or perform the re-pricing of one selection. Commits when it applies.

    ``start`` is inclusive and ``end`` exclusive, both UTC. A selection with
    neither a spool nor a period is refused (``ScopeRequired``): re-pricing
    everything is exactly the silent rewrite of history this door exists to
    avoid.
    """
    start, end = _naive(start), _naive(end)
    if spool_id is None and (start is None or end is None):
        raise ScopeRequired("a spool or a whole period")

    default_rate = await default_rate_per_kg(db)
    result = RepriceResult(dry_run=not apply)

    if apply:
        # The archives first, ascending — the order the usage tracker takes
        # them in before it books rows (archive, then spool rows). They are
        # read once without a lock only to learn which ones the selection names.
        archive_ids = sorted(
            {
                row.archive_id
                for row, _price in (await db.execute(_selection(spool_id, start, end))).all()
                if row.archive_id is not None
            }
        )
        for archive_id in archive_ids:
            await take_write_lock(db, PrintArchive.__table__, archive_id)
        if archive_ids:
            await db.execute(
                select(PrintArchive.id)
                .where(PrintArchive.id.in_(archive_ids))
                .order_by(PrintArchive.id)
                .with_for_update()
            )

    query = _selection(spool_id, start, end)
    if apply:
        query = query.with_for_update(of=SpoolUsageHistory).execution_options(populate_existing=True)
    selected = (await db.execute(query)).all()

    # What each archive's rows cost before and after. "No cost" and 0.00 are
    # the same money, so a row moves only when the figure does.
    deltas: dict[int, float] = {}
    for row, spool_price in selected:
        result.rows += 1
        before = row.cost
        after = usage_cost(row.weight_used, spool_price, default_rate)
        if before is not None and _booked_at_this_price(row.weight_used, before, spool_price, default_rate):
            after = before
        result.cost_before += before or 0.0
        result.cost_after += after or 0.0
        if round(after or 0.0, 2) == round(before or 0.0, 2):
            continue
        result.rows_changed += 1
        if row.archive_id is not None:
            deltas[row.archive_id] = deltas.get(row.archive_id, 0.0) + (after or 0.0) - (before or 0.0)
        if apply:
            row.cost = after

    moved = {archive_id: delta for archive_id, delta in deltas.items() if round(delta, 2) != 0}
    if moved:
        archives = (
            (await db.execute(select(PrintArchive).where(PrintArchive.id.in_(moved)).order_by(PrintArchive.id)))
            .scalars()
            .all()
        )
        orders: set[int] = set()
        for archive in archives:
            new_cost = round(max(0.0, (archive.cost or 0.0) + moved[archive.id]), 2)
            if new_cost == archive.cost:
                continue
            result.archives_changed += 1
            if archive.project_id is not None:
                orders.add(archive.project_id)
            if apply:
                archive.cost = new_cost
        result.orders_changed = len(orders)

    result.cost_before = round(result.cost_before, 2)
    result.cost_after = round(result.cost_after, 2)

    if apply:
        await db.commit()
        logger.info(
            "Re-priced usage (spool=%s, from=%s, to=%s): %d of %d rows, %d archives, %d orders, %.2f -> %.2f",
            spool_id,
            start,
            end,
            result.rows_changed,
            result.rows,
            result.archives_changed,
            result.orders_changed,
            result.cost_before,
            result.cost_after,
        )
    return result


async def spool_exists(db: AsyncSession, spool_id: int) -> bool:
    return bool(await db.scalar(select(func.count()).select_from(Spool).where(Spool.id == spool_id)))
