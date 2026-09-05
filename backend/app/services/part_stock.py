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

**An archive's credit and its reversal are one pair, kept together.**
:func:`credit_unfiled_print` and :func:`reverse_unfiled_print` live here rather
than in the completion handler or the archive route because the two only make
sense read against each other: the first is idempotent on "this PART's share of
this archive is already on the shelf", the second on "the sum of everything
naming it is already zero". Split across two modules, one of them would
eventually be changed without the other and a print filed after the fact would
count twice — which is the exact bug Decision 3 exists to prevent.

**The writer also follows a part that stops existing.** ``product_part_id`` is
ON DELETE CASCADE, which PostgreSQL honours and SQLite does not (this codebase
never sets ``PRAGMA foreign_keys = ON``), so :func:`delete_for_part` and
:func:`repoint` exist for the routes that delete and merge parts. Left to the
FK, a deleted part's movements would survive on SQLite and count towards a part
nothing can name any more.
"""

import logging
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart
from backend.app.models.part_stock import ProductPartStockMovement
from backend.app.models.product import ProductPart, ProductPlate
from backend.app.models.project_line import ProjectLine
from backend.app.services.order_metrics import IN_CHUNK, index_plates, products_for_print, row_quantity
from backend.app.services.product_composition import part_index

logger = logging.getLogger(__name__)

#: The two reasons that make up a line's reservation. Read as ONE sum: a
#: release is not the deletion of a reservation but a movement of its own, so
#: "what is still reserved" is ``−Σ(both)`` and never "the last row wins".
_RESERVATION_REASONS = ("reserved_for_order", "reservation_released")

#: Why the BACKEND wrote a movement — a closed set of tokens, never a sentence
#: (Ruling 17). The product page renders each of these in the operator's own
#: language, and an English sentence baked into the row is untranslatable
#: forever: it is written once and read for the life of the ledger. The only
#: free-text note is the operator's own ``manual`` adjustment, which is their
#: sentence and not ours.
#:
#: ⚠️ Closed. A new backend-written note is a new constant here AND a new
#: string in both locale files — a test pins that every ``NOTE_*`` constant in
#: this module is a member and that every member has a constant, so an
#: unlisted token cannot reach the table.
NOTE_ORDER_CANCELLED = "order_cancelled"
NOTE_LINE_DELETED = "line_deleted"
NOTE_PROJECT_DELETED = "project_deleted"
NOTE_RESERVATION_REWRITTEN = "reservation_rewritten"
NOTE_FILED_UNDER_ORDER = "filed_under_order"
NOTE_UNFILED_FROM_ORDER = "unfiled_from_order"
NOTE_COUNTED_BY_OPERATOR = "counted_by_operator"

NOTE_TOKENS = (
    NOTE_ORDER_CANCELLED,
    NOTE_LINE_DELETED,
    NOTE_PROJECT_DELETED,
    NOTE_RESERVATION_REWRITTEN,
    NOTE_FILED_UNDER_ORDER,
    NOTE_UNFILED_FROM_ORDER,
    NOTE_COUNTED_BY_OPERATOR,
)

#: The one archive status a print may be credited from. Spelled here rather
#: than imported from ``order_metrics._DONE`` because that name is private;
#: the two are pinned together by ``credit_unfiled_print``'s tests.
_COMPLETED = "completed"

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


async def balances_for_products(db: AsyncSession, product_ids: Sequence[int]) -> dict[int, dict[int, int]]:
    """:func:`balances` for MANY products at once — ``product_id → {part_id: Σ}``.

    ⚠️ **One statement for the whole page.** The products list shows every
    product's ``kits_available``, and calling :func:`balances` per row would put
    an N+1 over the ledger behind the catalog — the pass-6 discipline that
    ``_plates_count`` and the order loaders already follow. Chunked by
    ``order_metrics.IN_CHUNK`` for the same reason every other ``IN (...)`` list
    here is, so a farm with thousands of products does not build one enormous
    statement; a page of products is one chunk and therefore one statement, and
    ``test_the_products_list_reads_the_stock_ledger_once`` pins that.

    Same filter as :func:`balances` — counted parts only, and every one of them
    present with a 0 where nothing moved. A product whose parts are all
    purchased is ABSENT from the answer rather than mapped to an empty dict:
    the caller reads ``.get(product_id, {})`` and :func:`kits_available` says 0
    to an empty map anyway.
    """
    if not product_ids:
        return {}
    per_product: dict[int, dict[int, int]] = {}
    for start in range(0, len(product_ids), IN_CHUNK):
        rows = await db.execute(
            select(
                ProductPart.product_id,
                ProductPart.id,
                func.coalesce(func.sum(ProductPartStockMovement.delta), 0),
            )
            .outerjoin(ProductPartStockMovement, ProductPartStockMovement.product_part_id == ProductPart.id)
            .where(
                ProductPart.product_id.in_(product_ids[start : start + IN_CHUNK]),
                ProductPart.kind == "printed",
                ProductPart.qty_per_unit > 0,
            )
            .group_by(ProductPart.product_id, ProductPart.id)
        )
        for product_id, part_id, total in rows.all():
            per_product.setdefault(product_id, {})[part_id] = int(total or 0)
    return per_product


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

    ``populate_existing`` because the identity map would otherwise hand back a
    part loaded earlier in the same transaction, ``kind`` and ``qty_per_unit``
    as they were then — and those two ARE :func:`is_counted`, the question this
    SELECT is taken for. A route that edits a part and then moves stock on it
    would decide against the pre-edit row.
    """
    return (
        select(ProductPart).where(ProductPart.id == part_id).with_for_update().execution_options(populate_existing=True)
    )


async def lock_parts(db: AsyncSession, parts: Sequence[ProductPart]) -> None:
    """Take :func:`lock_part_stmt` on every one of ``parts``, lowest id first.

    Every caller that DECIDES something across several parts — how many kits a
    line may reserve, how much of a surplus is still unbanked, whether an
    archive's parts are already standing on the shelf — has to hold the lock
    before it reads, or two transactions each decide against a shelf the other
    is about to empty. ``move`` locks the one part it writes, which is too late
    for a decision taken over all of them at once.

    Id order is the whole safety of it: two of these loops can never take the
    same two locks in opposite orders, so they queue instead of deadlocking.
    Sorted here rather than trusted from the caller, because the callers get
    their parts from three different queries.

    A no-op on SQLite (its dialect emits no ``FOR UPDATE``), whose single write
    lock already serialises what this is for.
    """
    for part in sorted(parts, key=lambda part: part.id):
        await db.execute(lock_part_stmt(part.id))


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


async def counted_parts_of(db: AsyncSession, product_ids: Sequence[int]) -> list[ProductPart]:
    """The counted parts of these products, lowest id first.

    Id order is what a reservation is written in — and what its locks are taken
    in, so two transactions touching the same products can never take them the
    other way round and deadlock.

    ``populate_existing`` because ``kind`` and ``qty_per_unit`` ARE
    :func:`is_counted`: a route that edited a part earlier in the same
    transaction must not have this question answered from the identity map's
    pre-edit copy.

    Public because the banking route locks a whole ORDER's products before it
    decides anything (finding M5), and it has no business spelling
    :func:`is_counted` a second time to do it.
    """
    if not product_ids:
        return []
    rows = (
        (
            await db.execute(
                select(ProductPart)
                .where(ProductPart.product_id.in_(product_ids))
                .order_by(ProductPart.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    return [part for part in rows if is_counted(part)]


async def _counted_parts(db: AsyncSession, product_id: int) -> list[ProductPart]:
    """:func:`counted_parts_of` for the one-product callers."""
    return await counted_parts_of(db, [product_id])


async def _reserved_net_by_part(db: AsyncSession, line_id: int) -> list[tuple[int, int]]:
    """``(part_id, units still reserved)`` for one line, from the ledger.

    ``−Σ delta`` over the line's ``reserved_for_order`` and
    ``reservation_released`` rows: a reservation of 3 that was released reads
    zero, and one released twice by a bug reads zero rather than negative
    (the ``> 0`` filter). Parts with nothing outstanding are absent.
    """
    rows = (
        await db.execute(
            select(
                ProductPartStockMovement.product_part_id,
                func.coalesce(func.sum(ProductPartStockMovement.delta), 0),
            )
            .where(
                ProductPartStockMovement.project_line_id == line_id,
                ProductPartStockMovement.reason.in_(_RESERVATION_REASONS),
            )
            .group_by(ProductPartStockMovement.product_part_id)
            .order_by(ProductPartStockMovement.product_part_id)
        )
    ).all()
    return [(part_id, -int(net or 0)) for part_id, net in rows if int(net or 0) < 0]


async def release_for_line(db: AsyncSession, line: ProjectLine, *, note: str) -> None:
    """Put a line's reservation back on the shelf (Decision 4).

    What is released is what the LEDGER says is still reserved — ``Σ reserved
    − Σ released`` per part — never ``quantity × qty_per_unit`` recomputed from
    the line. The line's quantity may have changed since, the reservation may
    have been clamped when it was taken, and a part may have been merged into
    another; only the ledger knows what actually came off the shelf, and
    releasing a recomputed number is how a ledger starts inventing stock.

    Nothing reserved writes nothing: pressing cancel on an order whose lines
    never reserved leaves no trail of zeroes (Decision 6 puts this table in
    front of the operator).

    ``note`` is required and not defaulted because the product page shows it,
    and "why did these parts come back" is the only question that history
    answers. It is a token from :data:`NOTE_TOKENS`, never a sentence — the
    page renders it in the operator's language (Ruling 17).

    A part that has since stopped counting is skipped and logged: it has no
    balance to return to, and :func:`move` would refuse it as a caller error.
    Its stock stays out until the part is a counted part again — which is the
    same thing the reversal of an unfiled print does for the same reason.

    Flushes through :func:`move`; the caller commits.
    """
    outstanding = await _reserved_net_by_part(db, line.id)
    if not outstanding:
        return
    parts = {
        part.id: part
        for part in (
            await db.execute(
                select(ProductPart)
                .where(ProductPart.id.in_([pid for pid, _n in outstanding]))
                # ``populate_existing`` for the same reason ``lock_part_stmt``
                # carries it: ``kind`` and ``qty_per_unit`` ARE :func:`is_counted`,
                # and a route that edited a part earlier in this transaction
                # would otherwise be answered from the identity map with the
                # pre-edit row.
                .execution_options(populate_existing=True)
            )
        ).scalars()
    }
    for part_id, units in outstanding:
        part = parts.get(part_id)
        if part is None or not is_counted(part):
            logger.info(
                "part_stock: part %s no longer holds stock; line %s's reservation of %d is not released",
                part_id,
                line.id,
                units,
            )
            continue
        await move(
            db,
            part_id=part_id,
            delta=units,
            reason="reservation_released",
            project_line_id=line.id,
            note=note,
        )


async def reserve_for_line(db: AsyncSession, line: ProjectLine, units: int, *, created_by: int | None = None) -> int:
    """Take ``units`` whole kits off the product's shelf for this line (Decision 4).

    **Release first, then decide.** The product's balance already has THIS
    line's own reservation subtracted from it, so computing
    :func:`kits_available` before the release would make an edit from 3 to 3 a
    reservation of 0 — the line would be bidding against itself. Rewriting is
    release + reserve inside the caller's one transaction, which is why
    :func:`move` never commits.

    **The answer may be smaller than the question**, for two reasons:

    * Ruling 1 — the line dialog renders a default and the operator presses OK
      some minutes later, by which time another order may have taken the shelf.
      Asking for 5 of 3 reserves 3 and returns 3: the response tells the dialog
      what it got, and no balance ever goes negative.
    * Ruling 16 — a reservation never exceeds ``line.quantity``. Holding kits a
      line cannot use is stock withheld from every other order for nothing, and
      it made the order-level ``complete`` count a spare kit of a one-unit line
      as if a ten-unit sibling could assemble it.

    So ``take = min(units, line.quantity, kits_available(...))``.

    Returns the kits actually reserved, read back from the movements written
    rather than from the number asked for — the same discipline the banking
    button follows, so the day a rule is added to :func:`move` the caller
    follows the shelf instead of its own request.

    A product with no counted part reserves nothing and says 0: there is no kit
    to be short of, exactly as :func:`kits_available` answers.

    Flushes through :func:`move`; the caller commits.
    """
    if units < 0:
        raise ValueError(f"cannot reserve {units} units for line {line.id}; a reservation is never negative")
    await release_for_line(db, line, note=NOTE_RESERVATION_REWRITTEN)
    parts = await _counted_parts(db, line.product_id)
    if not parts or units == 0:
        return 0
    # ⚠️ Lock every counted part BEFORE the balances are read (finding I1).
    # ``move`` locks the one part it is about to write, but the KIT DECISION is
    # made across all of them at once and one part at a time is too late: two
    # transactions would each read the same shelf, each decide three kits are
    # free, and the clamp in ``move`` would fire per part on a decision already
    # taken. Locking here means the second transaction waits for the first to
    # commit and then reads the true balances.
    await lock_parts(db, parts)
    # After the release, so the shelf includes what this line was holding.
    take = min(units, line.quantity, kits_available(await balances(db, line.product_id), parts))
    if take <= 0:
        return 0
    reserved = take
    for part in parts:
        movement = await move(
            db,
            part_id=part.id,
            delta=-take * part.qty_per_unit,
            reason="reserved_for_order",
            project_line_id=line.id,
            created_by=created_by,
        )
        # ⚠️ No ``archive_id`` on a reservation, ever: ``reverse_unfiled_print``
        # negates EVERY row carrying an archive id, and a reservation caught in
        # that sum would be handed back as stock the print never made.
        reserved = min(reserved, 0 if movement is None else -movement.delta // part.qty_per_unit)
    return reserved


@dataclass(slots=True)
class LineStockReads:
    """Everything the order figures need out of the ledger, from ONE read.

    ``reserved_units`` is ``line_id → kits still held off the shelf``;
    ``banked_by_part`` is ``(line_id, part_id) → Σ surplus_banked``, the number
    the banking button subtracts from a line's surplus so a second press moves
    only what has appeared since (Ruling 30).

    The two travel together because they come out of one grouped statement over
    ``product_part_stock_movements``, and they must keep doing so: the order
    page, the orders list, the products list and the plan candidates each hold a
    spy pinning that table to a single read per response (the pass-6 batch
    discipline). Two "obviously independent" helpers here would be two reads.
    """

    reserved_units: dict[int, int]
    banked_by_part: dict[tuple[int, int], int]


async def line_ledger_reads(
    db: AsyncSession, line_ids: Sequence[int], qty_per_unit: Mapping[int, int]
) -> LineStockReads:
    """The reservation AND the banked surplus of many lines, in ONE query.

    The reservation has no column on ``project_lines`` (Decision 4): it IS the
    ledger, so every reader derives it the same way — ``−Σ delta`` over the
    line's reservation rows, divided by that part's ``qty_per_unit``, and the
    scarcest part wins because a kit is the unit the operator reserves in. The
    banked half is read the same way and for the same reason: the route that
    banks used to run a private ``SELECT`` for it, which is a second answer to
    "what has this line already put on the shelf" and one the button could not
    see (Ruling 30 — it gated on a surplus that banking never lowered).

    ⚠️ **One query for every line of the page**, because both order loaders
    call this for a whole batch of orders (the pass-6 discipline). A per-line
    variant would put an N+1 under the orders list, the customer page and every
    product endpoint at once. ``GROUP BY (line, part, reason)`` and the split in
    Python rather than CASE-sums, because "this part has no reservation row at
    all" and "its reservation nets to zero" are different facts and only the
    first must be kept out of the ``min`` below.

    Only lines that actually hold something appear in ``reserved_units`` — a
    caller reads ``.get(line_id, 0)``. ``qty_per_unit`` is the caller's own map
    of the parts it has already loaded; a part missing from it is skipped rather
    than guessed at, and a line left with no readable part reads 0.
    """
    if not line_ids:
        return LineStockReads(reserved_units={}, banked_by_part={})
    reserved_net: dict[tuple[int, int], int] = defaultdict(int)
    banked: dict[tuple[int, int], int] = defaultdict(int)
    for start in range(0, len(line_ids), IN_CHUNK):
        rows = (
            await db.execute(
                select(
                    ProductPartStockMovement.project_line_id,
                    ProductPartStockMovement.product_part_id,
                    ProductPartStockMovement.reason,
                    func.coalesce(func.sum(ProductPartStockMovement.delta), 0),
                )
                .where(
                    ProductPartStockMovement.project_line_id.in_(line_ids[start : start + IN_CHUNK]),
                    ProductPartStockMovement.reason.in_((*_RESERVATION_REASONS, "surplus_banked")),
                )
                .group_by(
                    ProductPartStockMovement.project_line_id,
                    ProductPartStockMovement.product_part_id,
                    ProductPartStockMovement.reason,
                )
            )
        ).all()
        for line_id, part_id, reason, net in rows:
            if reason == "surplus_banked":
                banked[(line_id, part_id)] += int(net or 0)
            else:
                reserved_net[(line_id, part_id)] += int(net or 0)
    per_line: dict[int, list[int]] = defaultdict(list)
    for (line_id, part_id), net in reserved_net.items():
        qty = qty_per_unit.get(part_id)
        if not qty:
            continue
        per_line[line_id].append(max(0, -net) // qty)
    return LineStockReads(
        reserved_units={line_id: kits for line_id, taken in per_line.items() if (kits := min(taken)) > 0},
        banked_by_part={key: net for key, net in banked.items() if net > 0},
    )


async def reserved_units_by_line(
    db: AsyncSession, line_ids: Sequence[int], qty_per_unit: Mapping[int, int]
) -> dict[int, int]:
    """The reservation half of :func:`line_ledger_reads`, for callers that ask
    nothing about banking."""
    return (await line_ledger_reads(db, line_ids, qty_per_unit)).reserved_units


async def reserved_units_for_line(db: AsyncSession, line: ProjectLine) -> int:
    """How many kits ONE line is currently holding off the shelf.

    A thin wrapper over :func:`reserved_units_by_line` and nothing else, so
    "kits held" has exactly one derivation. The route that lowers a line's
    quantity has to ask this question, and a private ``SELECT`` there would be
    a second answer to it — which would disagree the first time a part was
    merged or a release was half-written.
    """
    parts = await _counted_parts(db, line.product_id)
    if not parts:
        return 0
    held = await reserved_units_by_line(db, [line.id], {part.id: part.qty_per_unit for part in parts})
    return held.get(line.id, 0)


async def detach_line(db: AsyncSession, line_id: int) -> int:
    """Cut a deleted order line out of the ledger, keeping its history.

    ``project_line_id`` is ON DELETE SET NULL and PostgreSQL would do this
    itself; SQLite would not (this codebase never sets ``PRAGMA foreign_keys =
    ON``), and the rows it left would name a line id that no longer exists —
    counted as "already banked" against whatever line inherits that rowid on a
    fresh install, and offered as a dead link from the product's history.

    Called AFTER :func:`release_for_line`, never instead of it: detaching first
    would hide the reservation from the release query and leave the stock off
    the shelf forever. What survives is the ``surplus_banked`` and released
    rows with no line — the parts are on the shelf regardless of what happened
    to the paperwork.
    """
    result = await db.execute(
        update(ProductPartStockMovement)
        .where(ProductPartStockMovement.project_line_id == line_id)
        .values(project_line_id=None)
    )
    return result.rowcount or 0


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


async def delete_for_parts(db: AsyncSession, part_ids: Sequence[int]) -> int:
    """:func:`delete_for_part` for a whole product's parts at once.

    The product-delete route drops every part in one go and would otherwise
    loop this table once per part. Same reason as its single sibling: the FK
    cascade is PostgreSQL's, and on SQLite a fresh install REUSES rowids — the
    orphaned ledger would not merely linger, it would eventually attach itself
    to whatever part inherited the id.
    """
    if not part_ids:
        return 0
    result = await db.execute(
        delete(ProductPartStockMovement).where(ProductPartStockMovement.product_part_id.in_(part_ids))
    )
    return result.rowcount or 0


async def detach_archive(db: AsyncSession, archive_id: int) -> int:
    """Cut an archive out of the ledger without touching the stock it made.

    For the hard-delete path. ``archive_id`` is ON DELETE SET NULL — the parts
    are on the shelf whatever happened to the print history (spec §Invariants
    touched: "deleting an archive does not delete its movements") — and that
    clause fires on PostgreSQL only. On SQLite the movements would keep an id
    that names nothing: a dead link in the product's history table, and worse,
    an idempotency key that could be reused by the next archive to take that
    rowid, which would silently suppress ITS credit.
    """
    result = await db.execute(
        update(ProductPartStockMovement)
        .where(ProductPartStockMovement.archive_id == archive_id)
        .values(archive_id=None)
    )
    return result.rowcount or 0


async def detach_archives(db: AsyncSession, archive_ids: Sequence[int]) -> int:
    """:func:`detach_archive` for a whole batch of archives at once.

    For the BULK hard-delete paths, which never go through
    ``ArchiveService.delete_archive`` and so never reach its single-archive
    detach — deleting a user with ``delete_items`` is one Core ``DELETE`` over
    every archive they created (finding I2). Left attached, those movements keep
    an ``archive_id`` naming a row that no longer exists, and on SQLite the next
    archive to inherit that rowid finds its own credit already "standing" and is
    silently never counted into stock.

    Chunked like every other ``IN (...)`` list here: a user who has been running
    a farm for two years can own tens of thousands of prints, and SQLite refuses
    past 32766 bound parameters.
    """
    if not archive_ids:
        return 0
    detached = 0
    for start in range(0, len(archive_ids), IN_CHUNK):
        result = await db.execute(
            update(ProductPartStockMovement)
            .where(ProductPartStockMovement.archive_id.in_(archive_ids[start : start + IN_CHUNK]))
            .values(archive_id=None)
        )
        detached += result.rowcount or 0
    return detached


async def unfiled_credit_net(db: AsyncSession, archive_id: int) -> int:
    """How much free stock this archive is currently holding on the shelf.

    ``Σ delta`` over EVERY movement carrying this ``archive_id`` — the credit,
    its reversal when the print was filed under an order, and the re-credit
    when it was un-filed again. That sum, not the existence of a row, is what
    "already counted" means: an archive that was credited and then filed has
    rows but holds nothing, and crediting it again after un-filing is right.

    One function, two readers (the writer's own idempotency check and the
    archive route's 409) — a second copy of the query is a second answer to
    "is this print already on the shelf", and they would disagree the first
    time somebody un-filed a print.
    """
    return int(
        await db.scalar(
            select(func.coalesce(func.sum(ProductPartStockMovement.delta), 0)).where(
                ProductPartStockMovement.archive_id == archive_id
            )
        )
        or 0
    )


async def _net_by_part(db: AsyncSession, archive_id: int) -> dict[int, int]:
    """``part_id → Σ delta`` of everything carrying this ``archive_id``.

    :func:`unfiled_credit_net` split by part — the same sum, asked per part
    because that is the grain both idempotency questions are decided at
    (Ruling 28): which parts of this print are still standing on the shelf, and
    which of them a reversal has anything to take back.

    ⚠️ This counts EVERY row carrying the id, whatever its reason. Only
    ``unfiled_print`` and its own reversals ever set that column — do not set it
    on movements of another reason without revisiting both readers.
    """
    rows = (
        await db.execute(
            select(
                ProductPartStockMovement.product_part_id,
                func.coalesce(func.sum(ProductPartStockMovement.delta), 0),
            )
            .where(ProductPartStockMovement.archive_id == archive_id)
            .group_by(ProductPartStockMovement.product_part_id)
            .order_by(ProductPartStockMovement.product_part_id)
        )
    ).all()
    return {part_id: int(net or 0) for part_id, net in rows}


async def credit_unfiled_print(
    db: AsyncSession, archive: PrintArchive, *, created_by: int | None = None, note: str | None = None
) -> list[ProductPartStockMovement]:
    """Put a print that belongs to no order onto the shelf (Decision 3).

    One movement per product part, ``printed − defective`` summed over the
    archive's rows that resolve to it. The mapping is order attribution's own,
    borrowed rather than restated: :func:`order_metrics.products_for_print` for
    plate → product, ``product_composition.part_index`` for object name → part,
    :func:`order_metrics.row_quantity` for what a finished row handed over. A
    row no product counts is skipped — the same silence a ``qty_per_unit = 0``
    part gets from attribution, because it is the same statement: the product
    does not measure this object.

    **Idempotent PER PART, by that part's net for this archive** (Ruling 28),
    not by a row existing and not by the archive's total. A second completion
    event for the same print (an MQTT replay, a reconnect flap) finds stock
    already standing on every part and writes nothing — and a print that was
    credited, FILED under an order (net back to zero) and then un-filed again is
    credited afresh, which is the whole point of Ruling 11. That check is why
    ``archive_id`` carries an index of its own.

    ⚠️ Per part and not per archive because a reversal can be PARTIAL:
    :func:`reverse_unfiled_print` writes back every part whose stock is still
    there and refuses the ones already spent, so the archive's total stays above
    zero while some of its parts hold nothing. Read whole, that total said "all
    of it is already on the shelf" and the un-filing credited nothing at all —
    the reversed parts stayed lost. Read per part, each one answers for itself.

    Silent (an empty list, not an exception) for a print this does not apply
    to: one filed under an order, one that did not finish, one whose file no
    product claims. The caller is the completion handler, where a raise would
    be noise about a print that is simply not stock.

    ``created_by`` is the operator who asked, when one did — the archive page's
    "count this into stock". The completion handler passes nothing: it writes
    with no user (Decision 7). ``note`` says WHICH of the three ways this print
    reached the shelf, as a token from :data:`NOTE_TOKENS` (Ruling 17): the
    completion handler passes none (an ordinary credit needs no explanation),
    un-filing passes ``NOTE_UNFILED_FROM_ORDER`` and the archive page's manual
    action ``NOTE_COUNTED_BY_OPERATOR``.

    Flushes through :func:`move`; the caller commits.
    """
    if archive.project_id is not None or archive.status != _COMPLETED or archive.library_file_id is None:
        return []

    rows = (
        (
            await db.execute(
                select(PrintArchivePart).where(PrintArchivePart.archive_id == archive.id).order_by(PrintArchivePart.id)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return []

    plates = (
        (await db.execute(select(ProductPlate).where(ProductPlate.library_file_id == archive.library_file_id)))
        .scalars()
        .all()
    )
    plate_product, whole_file_product = index_plates(plates)
    product_ids = products_for_print(
        plate_product,
        whole_file_product,
        library_file_id=archive.library_file_id,
        plate_index=archive.plate_index,
    )
    if not product_ids:
        logger.debug("part_stock: archive %s prints a plate no product claims; nothing to credit", archive.id)
        return []

    parts = (await db.execute(select(ProductPart).where(ProductPart.product_id.in_(product_ids)))).scalars().all()
    by_product: dict[int, list[ProductPart]] = defaultdict(list)
    for part in parts:
        by_product[part.product_id].append(part)
    # ``product_ids`` comes back sorted from ``index_plates``, so a shared file
    # whose object key sits in two products always credits the same one of them
    # — the spec's "a shared file's rows go to the product that owns each part
    # key", made deterministic. Crediting both would double physical parts that
    # only exist once.
    indexes = [part_index(by_product.get(product_id, [])) for product_id in product_ids]

    wanted: dict[int, int] = defaultdict(int)
    for row in rows:
        owner: ProductPart | None = None
        for index in indexes:
            candidate = index.get(row.name_key)
            if candidate is not None and is_counted(candidate):
                owner = candidate
                break
        if owner is None:
            logger.debug("part_stock: archive %s object %r resolves to no counted part", archive.id, row.name_key)
            continue
        wanted[owner.id] += row_quantity(row, archive.status)
    if not wanted:
        return []

    # ⚠️ Lock, THEN read the nets, then decide (finding M5). The parts this
    # print is about to credit are known only here — after its rows have been
    # resolved through the product index — so this is the first moment the lock
    # can be taken, and it has to be taken before the read or two completion
    # handlers for the same archive would each find nothing standing and each
    # write a credit.
    #
    # ⚠️ Still assumes completion events for ONE archive are handled
    # sequentially (one process, one handler) on SQLite, where the lock is a
    # no-op. A partial unique index is NOT the alternative: this writes one row
    # per PART per archive, and an un-file/re-credit cycle adds another set on
    # top — there is no column combination that is unique here.
    await lock_parts(db, [part for part in parts if part.id in wanted])
    standing = await _net_by_part(db, archive.id)

    written: list[ProductPartStockMovement] = []
    for part_id, quantity in wanted.items():
        if standing.get(part_id, 0) > 0:
            # This part's share of the print is already on the shelf.
            continue
        movement = await move(
            db,
            part_id=part_id,
            delta=quantity,
            reason="unfiled_print",
            archive_id=archive.id,
            note=note,
            created_by=created_by,
        )
        if movement is not None:
            written.append(movement)
    return written


async def credit_if_unfiled(
    db: AsyncSession, archive: PrintArchive, *, created_by: int | None = None, note: str | None = None
) -> list[ProductPartStockMovement]:
    """:func:`credit_unfiled_print` that can never fail its caller.

    The two doors a print can arrive at without anybody asking for stock — the
    completion hook and the late 3MF attach (Ruling 27) — have exactly the same
    rule: the print happened, and the bookkeeping is a consequence of it, never
    a condition on it. So everything the ledger might object to is caught and
    logged here, in ONE place, rather than in two hooks that would each decide
    what "best effort" means.

    Flushes through :func:`move`; the caller commits. A caller that WANTS the
    refusal — the operator's own «Порахувати в залишок», which answers 409 —
    calls :func:`credit_unfiled_print` directly.
    """
    try:
        return await credit_unfiled_print(db, archive, created_by=created_by, note=note)
    except Exception as e:  # noqa: BLE001 — a print must never fail on its own bookkeeping
        logger.warning("part_stock: free-stock credit failed for archive %s: %s", archive.id, e, exc_info=True)
        return []


async def reverse_unfiled_print(db: AsyncSession, archive: PrintArchive, note: str) -> list[ProductPartStockMovement]:
    """Take back what an archive put on the shelf, once (Decision 3).

    Filing a print under an order after the fact means the order's own figures
    now count those parts; leaving the free-stock credit standing would count
    them twice. The reversal is a ``manual`` movement with the negated balance
    and the same ``archive_id``, so the product's history reads as a pair
    rather than as a number that changed by itself.

    ``note`` is a token from :data:`NOTE_TOKENS` (Ruling 17) — in practice
    ``NOTE_FILED_UNDER_ORDER``, the only reason this reversal ever runs.

    **Idempotent by arithmetic, not by a flag**: the sum of everything carrying
    this ``archive_id`` is what gets negated, so a second filing finds zero and
    writes nothing — and a partly-corrected ledger is finished off rather than
    reversed twice.

    A part that has since stopped counting (turned purchased, zeroed) is
    skipped: it has no balance to take back, and ``move`` would refuse it as a
    caller error.

    **Every part is attempted.** One part's stock having been spent says
    nothing about the others, and stopping at the first refusal would leave the
    rest of the plate double-counted for no reason. The reversals that worked
    are written (flushed — the caller commits them), and one
    :class:`PartStockError` naming the refused part ids is raised at the end.
    Both callers log that and file the archive anyway; the operator corrects
    the named parts by hand from the product page.
    """
    # ⚠️ This negates EVERY row carrying this ``archive_id``, whatever its
    # reason — see :func:`_net_by_part`, which is that sum and is also what the
    # credit reads to decide a part is already standing.
    outstanding = {part_id: net for part_id, net in (await _net_by_part(db, archive.id)).items() if net > 0}
    if not outstanding:
        return []
    parts = {
        part.id: part
        for part in (await db.execute(select(ProductPart).where(ProductPart.id.in_(outstanding)))).scalars()
    }

    written: list[ProductPartStockMovement] = []
    refused: list[int] = []
    for part_id, net in outstanding.items():
        part = parts.get(part_id)
        if part is None or not is_counted(part):
            logger.info("part_stock: part %s no longer holds stock; archive %s not reversed on it", part_id, archive.id)
            continue
        try:
            movement = await move(db, part_id=part_id, delta=-net, reason="manual", archive_id=archive.id, note=note)
        except PartStockError:
            refused.append(part_id)
            continue
        if movement is not None:
            written.append(movement)
    if refused:
        raise PartStockError(
            f"archive {archive.id}: part(s) {', '.join(str(p) for p in refused)} had already spent the stock this "
            f"print put on the shelf; {len(written)} of {len(written) + len(refused)} reversal(s) were written"
        )
    return written


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
