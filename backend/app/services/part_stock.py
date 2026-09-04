"""The only writer of ``product_part_stock_movements`` (pass 8, Decisions 1, 7).

Four callers write stock: the order page's «Списати надлишок» button, the
completion handler for a print filed under no order, the order-line dialog's
reservation, and a hand correction on the product page. All four come through
:func:`move`, because all four have to answer the same three questions — is
the reason one the readers understand, may this movement take the balance
below zero, and what actually moved — and a caller that answers them for
itself will answer at least one of them differently.

**Counted parts.** A balance is kept only for a part the product actually
counts: ``kind == "printed"`` AND ``qty_per_unit > 0``. That is the same
predicate ``order_metrics._new_line_figures`` applies to a line's parts and
``plan_engine.line_yield`` applies to a plate's yield — a purchased part is
procurement, not stock, and ``qty_per_unit = 0`` is the product's own "present
on the plate, do not measure me". The predicate is replicated here rather than
imported because neither of those modules exposes it; :func:`is_counted` is
this pass's spelling of it, and the three must move together.

**Transactions belong to the caller.** :func:`move` adds and FLUSHES, it never
commits. Editing a reservation is "release + reserve in one transaction"
(Decision 4) and banking a surplus is one movement per part (Decision 2) — a
commit inside ``move`` would tear both into halves that can each survive
alone. The flush is what makes the new row visible to the balance query that
the next ``move`` in the same transaction runs.

**Never below zero.** The ledger refuses a movement that would make a part's
balance negative — there is no such thing as owing yourself parts. The one
concession is Decision 4's stale dialog: a ``reserved_for_order`` for more
than is there reserves what is there and says so, because the operator asked
for a reservation and the honest answer is a smaller one, not an error page.
"""

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.part_stock import ProductPartStockMovement
from backend.app.models.product import ProductPart

logger = logging.getLogger(__name__)

#: The closed set of reasons a movement may carry. Order is the reading order
#: of the spec, not a priority.
REASONS = (
    "surplus_banked",
    "unfiled_print",
    "reserved_for_order",
    "reservation_released",
    "manual",
)


class PartStockError(Exception):
    """A movement the ledger refuses. The route maps it to 409."""


def is_counted(part: ProductPart) -> bool:
    """Does this part have a stock balance at all.

    Mirrors ``order_metrics._new_line_figures`` and ``plan_engine.line_yield``:
    printed, and wanted in a quantity greater than zero.
    """
    return part.kind == "printed" and part.qty_per_unit > 0


async def balances(db: AsyncSession, product_id: int) -> dict[int, int]:
    """``part_id → Σ delta`` for every COUNTED part of the product.

    Every counted part is in the answer, including the ones with no movement
    at all (0) — the caller asking for a product's stock wants a complete map,
    and a missing key is the shape that turns into a ``KeyError`` or a wrong
    "no stock" somewhere downstream. A purchased part, or one the product
    zeroed, is absent: it has no balance to have.
    """
    rows = await db.execute(
        select(ProductPart.id, func.coalesce(func.sum(ProductPartStockMovement.delta), 0))
        .outerjoin(ProductPartStockMovement, ProductPartStockMovement.product_part_id == ProductPart.id)
        .where(ProductPart.product_id == product_id, ProductPart.kind == "printed", ProductPart.qty_per_unit > 0)
        .group_by(ProductPart.id)
    )
    return {part_id: int(total or 0) for part_id, total in rows.all()}


def kits_available(part_balances: dict[int, int], parts: list[ProductPart]) -> int:
    """How many whole units the free stock can already make.

    ``min`` over the counted parts of ``floor(balance / qty_per_unit)`` — the
    kit is the unit the operator thinks in (Decision 4), so five lids and three
    bases are three kits and the two spare lids stay lids. A product with no
    counted part makes no kits: there is nothing to be short of, and answering
    "unlimited" would offer stock nobody has.
    """
    counted = [p for p in parts if is_counted(p)]
    if not counted:
        return 0
    # ``max(0, …)`` cannot fire while the ledger refuses negative balances; it
    # is here so a hand-edited database degrades to "no kits" rather than to a
    # negative offer in the line dialog.
    return max(0, min(part_balances.get(p.id, 0) // p.qty_per_unit for p in counted))


async def _balance_of(db: AsyncSession, part_id: int) -> int:
    total = await db.scalar(
        select(func.coalesce(func.sum(ProductPartStockMovement.delta), 0)).where(
            ProductPartStockMovement.product_part_id == part_id
        )
    )
    return int(total or 0)


async def move(
    db: AsyncSession,
    *,
    part_id: int,
    delta: int,
    reason: str,
    project_line_id: int | None = None,
    archive_id: int | None = None,
    note: str | None = None,
    created_by: int | None = None,
) -> ProductPartStockMovement:
    """Write one movement and return it. Flushes; does NOT commit.

    The returned row is the record of what actually moved, which is not always
    what was asked for: a ``reserved_for_order`` whose dialog default went
    stale between rendering and pressing OK reserves ``min(requested,
    available)`` rather than pushing the balance negative, and the caller reads
    ``-movement.delta`` to learn what it got. Every other reason carries a
    positive delta by construction, so one that would go below zero is a bug in
    the caller and is refused loudly.

    Raises ``ValueError`` for an argument no reader could make sense of (an
    unknown reason, a part that does not exist) and :class:`PartStockError` for
    a movement the ledger refuses (a balance below zero).
    """
    if reason not in REASONS:
        raise ValueError(f"unknown stock movement reason {reason!r}; expected one of {REASONS}")
    part = await db.get(ProductPart, part_id)
    if part is None:
        # SQLite does not enforce the FK, so without this the row would be
        # written and then be invisible to every reader, which joins the part.
        raise ValueError(f"no product part {part_id}")

    balance = await _balance_of(db, part_id)
    if balance + delta < 0:
        if reason != "reserved_for_order":
            raise PartStockError(
                f"movement {delta:+d} ({reason}) would take part {part_id} to {balance + delta}; stock never goes below 0"
            )
        # Decision 4's stale default: reserve what is there, say so in the row.
        logger.info("part_stock: clamping reservation of %d on part %s to the %d available", -delta, part_id, balance)
        delta = -balance

    movement = ProductPartStockMovement(
        product_part_id=part_id,
        delta=delta,
        reason=reason,
        project_line_id=project_line_id,
        archive_id=archive_id,
        note=note,
        created_by=created_by,
    )
    db.add(movement)
    await db.flush()
    return movement


async def movements(db: AsyncSession, product_id: int, *, limit: int = 200) -> list[ProductPartStockMovement]:
    """The product's movements, newest first.

    Deliberately NOT filtered to counted parts, unlike :func:`balances`: a
    movement written before a part was zeroed or turned purchased still
    happened, and a history that hides it is not a history. ``id`` breaks the
    tie because ``created_at`` is a second-resolution server default and a
    banking run writes several rows inside one.
    """
    rows = await db.execute(
        select(ProductPartStockMovement)
        .join(ProductPart, ProductPart.id == ProductPartStockMovement.product_part_id)
        .where(ProductPart.product_id == product_id)
        .order_by(ProductPartStockMovement.created_at.desc(), ProductPartStockMovement.id.desc())
        .limit(limit)
    )
    return list(rows.scalars().all())
