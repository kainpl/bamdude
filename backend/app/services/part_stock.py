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
sense read against each other: the first is idempotent on "an ``unfiled_print``
movement already names this archive", the second on "the sum of everything
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

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart
from backend.app.models.part_stock import ProductPartStockMovement
from backend.app.models.product import ProductPart, ProductPlate
from backend.app.models.project_line import ProjectLine
from backend.app.services.order_metrics import index_plates, products_for_print, row_quantity
from backend.app.services.product_composition import part_index

logger = logging.getLogger(__name__)

#: The two reasons that make up a line's reservation. Read as ONE sum: a
#: release is not the deletion of a reservation but a movement of its own, so
#: "what is still reserved" is ``−Σ(both)`` and never "the last row wins".
_RESERVATION_REASONS = ("reserved_for_order", "reservation_released")

#: How many ids go into one ``IN (...)`` list, matching ``order_metrics``' own
#: chunk. The batch reader below is asked about every line of a whole page of
#: orders, and SQLite refuses a statement past 32766 host parameters.
_IN_CHUNK = 500

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


async def _counted_parts(db: AsyncSession, product_id: int) -> list[ProductPart]:
    """The product's counted parts, lowest id first — the order a reservation
    is written in, so a ledger read back reads the same way twice."""
    rows = (
        (await db.execute(select(ProductPart).where(ProductPart.product_id == product_id).order_by(ProductPart.id)))
        .scalars()
        .all()
    )
    return [part for part in rows if is_counted(part)]


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
    answers — "reservation rewritten", "order cancelled", "line deleted".

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
            await db.execute(select(ProductPart).where(ProductPart.id.in_([pid for pid, _n in outstanding])))
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

    **The answer may be smaller than the question** (Ruling 1): the line dialog
    renders a default and the operator presses OK some minutes later, by which
    time another order may have taken the shelf. Asking for 5 of 3 reserves 3
    and returns 3 — the response tells the dialog what it got, and no balance
    ever goes negative. Asking for more than the line ordered is NOT clamped
    here: the ledger records what came off the shelf, and how many of them the
    line still needs is ``order_metrics``' arithmetic, not the shelf's.

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
    await release_for_line(db, line, note="reservation rewritten")
    parts = await _counted_parts(db, line.product_id)
    if not parts or units == 0:
        return 0
    # After the release, so the shelf includes what this line was holding.
    take = min(units, kits_available(await balances(db, line.product_id), parts))
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


async def reserved_units_by_line(
    db: AsyncSession, line_ids: Sequence[int], qty_per_unit: Mapping[int, int]
) -> dict[int, int]:
    """``line_id → kits reserved from stock``, for many lines in ONE query.

    The reservation has no column on ``project_lines`` (Decision 4): it IS the
    ledger, so every reader derives it the same way — ``−Σ delta`` over the
    line's reservation rows, divided by that part's ``qty_per_unit``, and the
    scarcest part wins because a kit is the unit the operator reserves in.

    ⚠️ **One query for every line of the page**, because both order loaders
    call this for a whole batch of orders (the pass-6 discipline). A per-line
    variant would put an N+1 under the orders list, the customer page and every
    product endpoint at once.

    Only lines that actually hold something appear in the answer — a caller
    reads ``.get(line_id, 0)``. ``qty_per_unit`` is the caller's own map of the
    parts it has already loaded; a part missing from it is skipped rather than
    guessed at, and a line left with no readable part reads 0.
    """
    if not line_ids:
        return {}
    per_line: dict[int, list[int]] = defaultdict(list)
    for start in range(0, len(line_ids), _IN_CHUNK):
        rows = (
            await db.execute(
                select(
                    ProductPartStockMovement.project_line_id,
                    ProductPartStockMovement.product_part_id,
                    func.coalesce(func.sum(ProductPartStockMovement.delta), 0),
                )
                .where(
                    ProductPartStockMovement.project_line_id.in_(line_ids[start : start + _IN_CHUNK]),
                    ProductPartStockMovement.reason.in_(_RESERVATION_REASONS),
                )
                .group_by(ProductPartStockMovement.project_line_id, ProductPartStockMovement.product_part_id)
            )
        ).all()
        for line_id, part_id, net in rows:
            qty = qty_per_unit.get(part_id)
            if not qty:
                continue
            per_line[line_id].append(max(0, -int(net or 0)) // qty)
    return {line_id: kits for line_id, taken in per_line.items() if (kits := min(taken)) > 0}


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


async def credit_unfiled_print(
    db: AsyncSession, archive: PrintArchive, *, created_by: int | None = None
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

    **Idempotent by the archive's NET** (:func:`unfiled_credit_net`), not by a
    row existing. A second completion event for the same print (an MQTT replay,
    a reconnect flap) finds stock already standing against this archive and
    writes nothing — and a print that was credited, FILED under an order (net
    back to zero) and then un-filed again is credited afresh, which is the
    whole point of Ruling 11. That check is why ``archive_id`` carries an index
    of its own.

    Silent (an empty list, not an exception) for a print this does not apply
    to: one filed under an order, one that did not finish, one whose file no
    product claims. The caller is the completion handler, where a raise would
    be noise about a print that is simply not stock.

    ``created_by`` is the operator who asked, when one did — the archive page's
    "count this into stock". The completion handler passes nothing: it writes
    with no user (Decision 7).

    Flushes through :func:`move`; the caller commits.
    """
    if archive.project_id is not None or archive.status != _COMPLETED or archive.library_file_id is None:
        return []
    # ⚠️ Assumes completion events for ONE archive are handled sequentially (one
    # process, one handler). A partial unique index is NOT the alternative: this
    # writes one row per PART per archive, and an un-file/re-credit cycle adds
    # another set on top — there is no column combination that is unique here.
    if await unfiled_credit_net(db, archive.id) > 0:
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

    written: list[ProductPartStockMovement] = []
    for part_id, quantity in wanted.items():
        movement = await move(
            db,
            part_id=part_id,
            delta=quantity,
            reason="unfiled_print",
            archive_id=archive.id,
            created_by=created_by,
        )
        if movement is not None:
            written.append(movement)
    return written


async def reverse_unfiled_print(db: AsyncSession, archive: PrintArchive, note: str) -> list[ProductPartStockMovement]:
    """Take back what an archive put on the shelf, once (Decision 3).

    Filing a print under an order after the fact means the order's own figures
    now count those parts; leaving the free-stock credit standing would count
    them twice. The reversal is a ``manual`` movement with the negated balance
    and the same ``archive_id``, so the product's history reads as a pair
    rather than as a number that changed by itself.

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
    # reason. Only ``unfiled_print`` and its own reversals ever set that column
    # — do not set it on movements of another reason without revisiting this
    # sum, which would otherwise take back stock that came from somewhere else.
    totals = (
        await db.execute(
            select(
                ProductPartStockMovement.product_part_id,
                func.coalesce(func.sum(ProductPartStockMovement.delta), 0),
            )
            .where(ProductPartStockMovement.archive_id == archive.id)
            .group_by(ProductPartStockMovement.product_part_id)
            .order_by(ProductPartStockMovement.product_part_id)
        )
    ).all()
    outstanding = {part_id: int(net or 0) for part_id, net in totals if int(net or 0) > 0}
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
