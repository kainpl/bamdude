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

**Nothing is not a movement.** A delta of zero — asked for as zero, or clamped
to it because there was nothing left to reserve — writes no row and returns
``None``. A ledger of no-ops is a ledger nobody reads to the end, and Decision
6 puts this table in front of the operator.

**The writer also follows a part that stops existing.** ``product_part_id`` is
ON DELETE CASCADE, which PostgreSQL honours and SQLite does not (this codebase
never sets ``PRAGMA foreign_keys = ON``), so :func:`delete_for_part` and
:func:`repoint` exist for the routes that delete and merge parts. Left to the
FK, a deleted part's movements would survive on SQLite and count towards a part
nothing can name any more.
"""

import logging

from sqlalchemy import delete, func, select, update
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

#: Which way a reason is allowed to point. Banking a surplus, counting an
#: unfiled print and releasing a reservation all put parts IN; a reservation
#: takes them OUT. Only a hand correction may go either way, because only the
#: operator knows which way a miscount went. A reason pointing the wrong way is
#: a caller that has confused two operations, not stock that has moved — hence
#: ``ValueError`` and not :class:`PartStockError`.
_REQUIRED_SIGN = {
    "surplus_banked": 1,
    "unfiled_print": 1,
    "reservation_released": 1,
    "reserved_for_order": -1,
    "manual": 0,  # either
}


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


def lock_part_stmt(part_id: int):
    """The SELECT that takes the row lock every write rides behind.

    Two operators pressing «Зі складу» on the last three kits at the same
    moment both read a balance of 3 and both reserve it; the ledger then holds
    −6 against a stock of 3, and neither clamp fired because neither
    transaction could see the other. Locking the PART row (not the movements —
    a lock on rows that do not exist yet stops nothing) serialises the
    read-decide-write for that part.

    PostgreSQL emits ``FOR UPDATE`` and the second transaction waits.
    **SQLite's dialect emits nothing at all** — and needs to: its writes are
    already serialised by a single write lock over the whole database. So this
    is a no-op on the small backend and the fix on the large one, which is why
    the test compiles it against both dialects rather than trusting either.
    """
    return select(ProductPart).where(ProductPart.id == part_id).with_for_update()


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
) -> ProductPartStockMovement | None:
    """Write one movement and return it, or ``None`` when nothing moved.

    The returned row is the record of what actually moved, which is not always
    what was asked for: a ``reserved_for_order`` whose dialog default went
    stale between rendering and pressing OK reserves ``min(requested,
    available)`` rather than pushing the balance negative, and the caller reads
    ``-movement.delta`` to learn what it got. Every other reason carries a
    positive delta by construction, so one that would go below zero is a bug in
    the caller and is refused loudly.

    ``None`` means the movement was nothing: a delta of zero was asked for, or
    a reservation clamped to zero because the stock had already gone. Nothing
    is written in either case — a caller that needs to know whether it got a
    row must check, and one reading ``-movement.delta`` for what it reserved
    reads ``0`` out of a ``None``.

    Flushes; does **not** commit. ⚠️ ``part_id`` is NOT scoped to a product
    here — the service is handed a part, not a product and a part, so a route
    taking both from the URL must check that the part belongs to the product
    before calling, exactly as ``routes/products.py::_part`` already does.

    Raises ``ValueError`` for an argument no reader could make sense of (an
    unknown reason, a wrong sign for that reason, a part that does not exist or
    is not counted) and :class:`PartStockError` for a movement the ledger
    refuses (a balance below zero).
    """
    if reason not in REASONS:
        raise ValueError(f"unknown stock movement reason {reason!r}; expected one of {REASONS}")
    # The lock is taken before the balance is read and held to the caller's
    # commit, so a concurrent reservation cannot decide against the same stock.
    part = (await db.execute(lock_part_stmt(part_id))).scalar_one_or_none()
    if part is None:
        # SQLite does not enforce the FK, so without this the row would be
        # written and then be invisible to every reader, which joins the part.
        raise ValueError(f"no product part {part_id}")
    if not is_counted(part):
        # Only a counted part HAS a balance (``balances`` filters on exactly
        # this), so a movement against a purchased part or one the product
        # zeroed is a row nothing would ever read back.
        raise ValueError(f"part {part_id} is not a counted printed part; it has no stock")

    balance = await _balance_of(db, part_id)
    if balance + delta < 0:
        if reason != "reserved_for_order":
            raise PartStockError(
                f"movement {delta:+d} ({reason}) would take part {part_id} to {balance + delta}; "
                "stock never goes below 0"
            )
        # Decision 4's stale default: reserve what is there, say so in the row.
        logger.info("part_stock: clamping reservation of %d on part %s to the %d available", -delta, part_id, balance)
        delta = -balance

    if delta == 0:
        return None

    required = _REQUIRED_SIGN[reason]
    if required and (delta > 0) != (required > 0):
        raise ValueError(f"{reason} moves stock {'in' if required > 0 else 'out'}; got a delta of {delta:+d}")

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


async def delete_for_part(db: AsyncSession, part_id: int) -> int:
    """Drop a part's whole ledger. Returns how many rows went.

    For the route that deletes a part. The FK is ON DELETE CASCADE and
    PostgreSQL would do this by itself; SQLite would not, and the rows it left
    would belong to a part id that no longer names anything — invisible to
    every reader (all of which join ``product_parts``) and impossible to
    correct. The route calls this beside its existing ``ProjectProcurement``
    cleanup, for exactly the same reason.
    """
    result = await db.execute(
        delete(ProductPartStockMovement).where(ProductPartStockMovement.product_part_id == part_id)
    )
    return result.rowcount or 0


async def repoint(db: AsyncSession, *, from_part_id: int, to_part_id: int) -> int:
    """Move one part's movements onto another. Returns how many rows moved.

    For the route that MERGES two parts: the source row goes away and the stock
    it holds is real stock sitting on a shelf, so — unlike the procurement
    counts, which are deliberately dropped — it follows the part it was merged
    into. The target's balance is then the sum of both ledgers, which is what
    the shelf actually holds.

    Raises ``ValueError`` if there is something to move and the target cannot
    hold it (gone, or not a counted part). A merge with no stock behind it is
    always allowed: two purchased parts merging is an ordinary edit, and
    refusing it because neither has a balance would break it for nothing.
    """
    if from_part_id == to_part_id:
        return 0
    moving = await db.scalar(
        select(func.count())
        .select_from(ProductPartStockMovement)
        .where(ProductPartStockMovement.product_part_id == from_part_id)
    )
    if not moving:
        return 0
    target = await db.get(ProductPart, to_part_id)
    if target is None or not is_counted(target):
        raise ValueError(
            f"part {to_part_id} cannot hold stock, so part {from_part_id}'s {moving} movement(s) have nowhere to go"
        )
    result = await db.execute(
        update(ProductPartStockMovement)
        .where(ProductPartStockMovement.product_part_id == from_part_id)
        .values(product_part_id=to_part_id)
    )
    return result.rowcount or 0
