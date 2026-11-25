"""An order's goods assembled, received and issued in batches — one function reads, one performs
(spec workshop-order-issue, rules 11, 12, 24).

:func:`state` is what the issue window shows and what :func:`apply` checks against: the same
arithmetic for the display and for the act. ``apply`` reads it again under the locks, refuses any
number above it with a sentence (409 — never clamped: an issue is physical units handed over) and
writes nothing then; otherwise, per line, assemble → receive → write off → issue, one :class:`StockIssue` for
the whole request, the journal, and — asked and allowed — the order closed. It writes through the
ledgers' own writers only (``finished_stock``, ``part_stock``, ``stock_issues``) and never commits.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.database import take_write_lock
from backend.app.core.lock_ledger import ORDER, before_lock, ledger
from backend.app.models.product import Product, ProductPart
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine, ProjectLinePartStock
from backend.app.models.stock_issue import StockIssue
from backend.app.models.user import User
from backend.app.services import finished_stock, order_journal, part_stock, stock_issues
from backend.app.services.order_metrics import LineFigures, OrderContext, attribute, load_order_context
from backend.app.services.product_gate import product_gate
from backend.app.services.stock_issues import Recipient

# WS-13 E1 BL2: the set of products changed between the read and the gates.
ORDER_CHANGED = "The order changed while this was being saved — try again"


async def lock_order(db: AsyncSession, project_id: int) -> None:
    """The order row, ``FOR NO KEY UPDATE`` (spec WS-13 E1, BL0 class 3): before any
    stock position or line. A door that ends by changing the order — completing it,
    cancelling it — otherwise took the row by that late UPDATE, after the positions
    another door holds while it waits on the order: the same crosswise wait as a line
    before its position. ``NO KEY UPDATE`` is the mode an UPDATE of the row takes
    anyway, so the later UPDATE never upgrades it, and inserts that merely reference
    the order (journal, lines, issues) are not blocked by it."""
    held = ledger(db)
    if held.holds("projects", project_id):
        return
    before_lock(db, "projects", project_id, ORDER)  # the order monitor (WS-13 E1 BL2)
    await take_write_lock(db, Project.__table__, project_id)
    await db.execute(select(Project.id).where(Project.id == project_id).with_for_update(key_share=True))
    held.note("projects", project_id, ORDER)


async def lock_orders(db: AsyncSession, project_ids) -> None:
    """The orders a print leaves or joins, locked as receiving locks them (WS-13 E13 V04): the
    order row, ascending id, BEFORE C1 is read. Receiving takes the same row before it reads
    what the order's prints made, so an exit and a receipt never both pass on what they each
    read — whichever comes second reads the other's result. The class-3 lock of the protocol
    (products → projects → …): a door that holds it takes only higher classes after it."""
    for project_id in sorted({pid for pid in project_ids if pid is not None}):
        await lock_order(db, project_id)


class FulfilmentError(Exception):
    """A batch the order refuses; ``status`` is the HTTP answer."""

    def __init__(self, detail: str, status: int = 409) -> None:
        super().__init__(detail)
        self.status = status


@dataclass(frozen=True, slots=True)
class PartState:
    part_id: int
    name: str
    wanted: int
    can_receive: int
    held: int
    issued: int
    #: Written off under the order (spec workshop-order-issue-followups, rule 44).
    written_off: int = 0
    stock_qty: int = 0


@dataclass(frozen=True, slots=True)
class LineState:
    line_id: int
    product_name: str
    mode: str
    ordered: int
    from_finished: int
    #: The line's live kit reservation.
    kits_reserved: int
    can_assemble: int
    can_receive: int
    #: On the shelf under the order (spec rule 7).
    held: int
    issued: int
    #: Written off under the order — a parts line sums its parts' (followups, rule 44).
    written_off: int = 0
    #: A parts line's parts, each counted on its own; empty for a product line.
    parts: list[PartState] = field(default_factory=list)

    @property
    def fully_issued(self) -> bool:
        if self.mode == "parts":
            return all(part.issued >= part.wanted for part in self.parts)
        return self.issued >= self.ordered and all(part.issued >= part.wanted for part in self.parts)

    @property
    def fully_stocked(self) -> bool:
        """Everything ordered is on the shelf for the order or issued (spec
        workshop-order-issue-followups, rule 36) — what an order without a customer closes on."""
        if self.mode == "parts":
            return all(part.issued + part.held >= part.wanted for part in self.parts)
        return self.issued + self.held >= self.ordered and all(
            part.issued + part.held >= part.wanted for part in self.parts
        )


@dataclass(frozen=True, slots=True)
class OrderState:
    lines: list[LineState]
    ordered: int
    issued: int
    held: int
    fully_issued: bool
    #: The order's totals of what a batch could do now — the banner's numbers, so no
    #: reader adds the rows up (a parts line counts its parts).
    can_assemble: int = 0
    can_receive: int = 0
    can_issue: int = 0
    #: No customer: the order closes into free stock once everything is received
    #: (spec workshop-order-issue-followups, rules 35–36).
    closes_to_stock: bool = False
    #: What completing asks — fully issued, or fully on the shelf when it closes to stock (rule 40).
    can_complete: bool = False


@dataclass(frozen=True, slots=True)
class LineRequest:
    line_id: int
    assemble: int = 0
    receive: int = 0
    issue: int = 0
    #: A parts line's numbers: ``part_id → (receive, issue)``.
    parts: dict[int, tuple[int, int]] = field(default_factory=dict)
    #: Units written off (spec workshop-order-issue-followups, rule 46).
    write_off: int = 0
    #: A parts line's write-offs: ``part_id → n``.
    parts_write_off: dict[int, int] = field(default_factory=dict)

    def numbers(self) -> list[int]:
        return [
            self.assemble,
            self.receive,
            self.write_off,
            self.issue,
            *(n for pair in self.parts.values() for n in pair),
            *self.parts_write_off.values(),
        ]


def _order_parts(
    ctx: OrderContext, figures: dict[int, LineFigures], counters: dict[int, dict[int, ProjectLinePartStock]]
) -> tuple[dict[int, int], dict[int, int]]:
    """``(usable, received)`` per part over the WHOLE order: the good parts its prints
    made, and the parts its lines already received (a product line — ``received × per``).

    Receiving is capped by the order's prints, not by a line's share of them (final
    review C2): ``attribute`` hands prints out by each line's remaining need, and that
    share moves when a quantity, a take or a deletion changes the needs — a line's own
    ``received`` stays where it was, so a per-line bound alone received one print twice."""
    usable: dict[int, int] = defaultdict(int)
    received: dict[int, int] = defaultdict(int)
    for line in ctx.lines:
        figs = figures[line.id]
        for pf in figs.parts:
            usable[pf.part_id] += pf.usable
        if line.mode == "parts":
            for part_id, row in counters.get(line.id, {}).items():
                received[part_id] += row.received
        else:
            for pf in figs.parts:
                if pf.per > 0:
                    received[pf.part_id] += max(0, (line.received or 0) * pf.per - pf.stock_used_qty)
                    received[pf.part_id] += ctx.made_by_line_part.get((line.id, pf.part_id), 0)
    return usable, received


def _product_line(ctx: OrderContext, line, figs: LineFigures, name: str, room: dict[int, int], counters) -> LineState:
    kits = min(
        ctx.reserved_by_line.get(line.id, 0),
        max(
            0,
            line.quantity
            - (line.from_finished or 0)
            - (line.assembled or 0)
            - (line.received or 0)
            + (line.written_off or 0),
        ),
    )
    from_finished, assembled, received = line.from_finished or 0, line.assembled or 0, line.received or 0
    written_off = line.written_off or 0
    # What the shelf and the kits already cover is not printed work to receive; what is
    # received covers the printed units from before (defects recorded later can make
    # ``units_printed`` smaller than ``received`` — then there is simply nothing more).
    # A written-off unit is to be made again (followups, rule 47).
    uncovered = line.quantity - from_finished - (kits + assembled) + written_off
    kit = [pf for pf in figs.parts if pf.per > 0]
    if kit:
        partial = {pf.part_id: max(0, pf.allocated_qty - kits * pf.per) for pf in kit}
        can_receive = max(0, uncovered - received)
        # Attribution admits only prints compatible with THIS line's material and
        # composition. The order-wide budget additionally prevents receiving the
        # same output again after attribution moves it to a sibling line.
        can_receive = min(
            can_receive,
            *(
                (
                    min(
                        max(0, room[pf.part_id]),
                        max(
                            0,
                            pf.usable
                            - max(0, received * pf.per - pf.stock_used_qty)
                            - ctx.made_by_line_part.get((line.id, pf.part_id), 0),
                        ),
                    )
                    + partial[pf.part_id]
                )
                // pf.per
                for pf in kit
            ),
        )
        for pf in kit:
            room[pf.part_id] -= max(0, can_receive * pf.per - partial[pf.part_id])
    else:
        # Nothing to print (a kit of bought parts): its units are received as they come
        # (final review M4) — otherwise such an order could never be issued and closed.
        can_receive = max(0, uncovered - received)
    # A kit is assembled only into a unit the order still needs: past the quantity it
    # would leave finished units held for an order that closes (spec rule 12).
    covered = from_finished + assembled + received - written_off
    extras = []
    for pf in kit:
        if not pf.extra_qty:
            continue
        row = counters.get(pf.part_id)
        extra_received = row.received if row is not None else 0
        extra_written_off = row.written_off if row is not None else 0
        # The main receipt has first call on the loose allocation. The number
        # exposed for extras is what is left after that maximum receipt.
        stock = max(0, pf.allocated_qty - kits * pf.per - can_receive * pf.per)
        # Extras obey the same line eligibility and shared-output budget as the
        # main kit. Its maximum receipt has already claimed printed components.
        eligible = max(
            0,
            pf.usable
            - max(0, received * pf.per - pf.stock_used_qty)
            - ctx.made_by_line_part.get((line.id, pf.part_id), 0)
            - max(0, can_receive * pf.per - partial[pf.part_id]),
        )
        extra_can_receive = min(
            max(0, pf.extra_qty + extra_written_off - extra_received), min(max(0, room[pf.part_id]), eligible) + stock
        )
        from_stock = min(extra_can_receive, stock)
        room[pf.part_id] -= extra_can_receive - from_stock
        extras.append(
            PartState(
                part_id=pf.part_id,
                name=pf.name,
                wanted=pf.extra_qty,
                can_receive=extra_can_receive,
                held=part_stock.part_held(row) if row is not None else 0,
                issued=row.issued if row is not None else 0,
                written_off=extra_written_off,
                stock_qty=from_stock,
            )
        )
    return LineState(
        line_id=line.id,
        product_name=name,
        mode=line.mode,
        ordered=line.quantity,
        from_finished=from_finished,
        kits_reserved=kits,
        can_assemble=max(0, min(kits, line.quantity - covered)),
        can_receive=can_receive,
        held=finished_stock.held_units(line),
        issued=line.issued or 0,
        written_off=written_off,
        parts=extras,
    )


def _parts_line(
    line, figs: LineFigures, name: str, counters: dict[int, ProjectLinePartStock], room: dict[int, int]
) -> LineState:
    parts = []
    # The parts with a shelf — the ones the parts ledger can hold for the order.
    for pf in figs.parts:
        if pf.per <= 0 or not pf.shelf:
            continue
        row = counters.get(pf.part_id)
        received = row.received if row is not None else 0
        written_off = row.written_off if row is not None else 0
        # A written-off part is to be made again (followups, rule 47).
        can_receive = min(max(0, min(pf.per + written_off, pf.usable) - received), max(0, room[pf.part_id]))
        room[pf.part_id] -= can_receive
        parts.append(
            PartState(
                part_id=pf.part_id,
                name=pf.name,
                wanted=pf.per,
                can_receive=can_receive,
                held=part_stock.part_held(row) if row is not None else 0,
                issued=row.issued if row is not None else 0,
                written_off=written_off,
            )
        )
    return LineState(
        line_id=line.id,
        product_name=name,
        mode=line.mode,
        ordered=sum(part.wanted for part in parts),
        from_finished=0,
        kits_reserved=0,
        can_assemble=0,
        can_receive=0,
        held=sum(part.held for part in parts),
        issued=sum(part.issued for part in parts),
        written_off=sum(part.written_off for part in parts),
        parts=parts,
    )


async def moved_line_ids(db: AsyncSession, lines: Sequence[ProjectLine]) -> set[int]:
    """The lines whose stock has moved (spec rule 13): a product line assembled, received
    or issued something; a parts line received or issued a part. One read for the parts lines."""
    moved = {line.id for line in lines if line.mode != "parts" and finished_stock.moved(line)}
    counters = await part_stock.line_part_stock(
        db, [line.id for line in lines if line.mode == "parts" or line.extra_percentages]
    )
    for line_id, rows in counters.items():
        if any(row.received + row.issued > 0 for row in rows.values()):
            moved.add(line_id)
    return moved


async def load_context(db: AsyncSession, project: Project) -> OrderContext:
    """The order's context, as ``state`` reads it — for a reader that also needs the
    context itself (the issue dialog's configuration captions, WS-13 E6 H04)."""
    ctx = await load_order_context(db, project.id)
    if ctx is None:
        raise FulfilmentError("Project not found", 404)
    return ctx


async def state(
    db: AsyncSession,
    project: Project,
    *,
    to_stock: bool | None = None,
    ctx: OrderContext | None = None,
    figures: dict[int, LineFigures] | None = None,
) -> OrderState:
    """What each line can assemble, receive and issue now — the order's context and the
    parts lines' counters read once each, whatever the order's size. ``to_stock`` overrides
    "has no customer" for a request that changes the customer as it completes (rule 35).

    ``ctx`` — a context the caller already read with ``load_context``, computed over as is;
    ``figures`` may reuse attribution from that same context. Writers pass neither
    and continue reading and attributing under their own locks."""
    if ctx is None:
        ctx = await load_context(db, project)
    if figures is None:
        figures, _other = attribute(ctx)
    counters = await part_stock.line_part_stock(
        db, [line.id for line in ctx.lines if line.mode == "parts" or line.extra_percentages]
    )
    usable, received = _order_parts(ctx, figures, counters)
    # What the order's prints hold beyond what its lines received — shared out in line order.
    room: dict[int, int] = defaultdict(int, {pid: usable[pid] - received[pid] for pid in set(usable) | set(received)})
    lines = []
    for line in ctx.lines:
        product = ctx.products_by_id.get(line.product_id)
        name = product.name if product is not None else ""
        if line.mode == "parts":
            lines.append(_parts_line(line, figures[line.id], name, counters.get(line.id, {}), room))
        else:
            lines.append(_product_line(ctx, line, figures[line.id], name, room, counters.get(line.id, {})))
    closes = project.customer_id is None if to_stock is None else to_stock
    fully_issued = all(row.fully_issued for row in lines)
    return OrderState(
        lines=lines,
        ordered=sum(row.ordered + (sum(p.wanted for p in row.parts) if row.mode == "product" else 0) for row in lines),
        issued=sum(row.issued + (sum(p.issued for p in row.parts) if row.mode == "product" else 0) for row in lines),
        held=sum(row.held + (sum(p.held for p in row.parts) if row.mode == "product" else 0) for row in lines),
        fully_issued=fully_issued,
        closes_to_stock=closes,
        can_complete=all(row.fully_stocked for row in lines) if closes else fully_issued,
        can_assemble=sum(row.can_assemble for row in lines),
        can_receive=sum(row.can_receive + sum(p.can_receive for p in row.parts) for row in lines),
        can_issue=sum(
            row.held
            + row.can_assemble
            + row.can_receive
            + sum(p.can_receive + (p.held if row.mode == "product" else 0) for p in row.parts)
            for row in lines
        ),
    )


async def ensure_prints_can_leave(db: AsyncSession, project_id: int, archive_ids: Sequence[int]) -> None:
    """Refuse taking prints out of an order when the order's remaining prints would no
    longer cover what its lines received (final review C1): those parts are on the shelf
    as finished goods — or with the customer — and un-filing would credit them to the
    free parts shelf a second time. A print beyond what was received may leave."""
    ctx = await load_order_context(db, project_id)
    if ctx is None or not archive_ids:
        return
    from backend.app.services.order_recount import ensure_history_can_change

    await ensure_history_can_change(db, project_id, [a for a in ctx.archives if a.id in set(archive_ids)])
    counters = await part_stock.line_part_stock(
        db, [line.id for line in ctx.lines if line.mode == "parts" or line.extra_percentages]
    )
    _usable, received = _order_parts(ctx, attribute(ctx)[0], counters)
    if not any(received.values()):
        return
    leaving = set(archive_ids)
    ctx.archives = [archive for archive in ctx.archives if archive.id not in leaving]
    usable_after, _received = _order_parts(ctx, attribute(ctx)[0], counters)
    if any(usable_after.get(part_id, 0) < n for part_id, n in received.items() if n > 0):
        raise FulfilmentError("These prints went onto the shelf for the order — they cannot leave it")


def _check(row: LineState, request: LineRequest) -> None:
    """Refuse the first number above what ``row`` allows — the order is assemble, receive,
    issue, because what is issued may come from the first two of the same request."""
    name = row.product_name
    if row.mode == "parts" and (request.assemble or request.receive or request.issue or request.write_off):
        raise FulfilmentError(f"«{name}» is received and issued part by part", 422)
    if row.mode == "product" and not row.parts and (request.parts or request.parts_write_off):
        raise FulfilmentError(f"«{name}» is issued as whole units", 422)
    if row.parts or request.parts or request.parts_write_off:
        by_id = {part.part_id: part for part in row.parts}
        for part_id in sorted(set(request.parts) | set(request.parts_write_off)):
            part = by_id.get(part_id)
            if part is None:
                raise FulfilmentError(f"«{name}» does not count this part", 422)
            receive, issue = request.parts.get(part_id, (0, 0))
            write_off = request.parts_write_off.get(part_id, 0)
            if receive > part.can_receive:
                raise FulfilmentError(f"«{name}», {part.name}: only {part.can_receive} can be received")
            if write_off > part.held + receive:
                raise FulfilmentError(f"«{name}», {part.name}: only {part.held + receive} can be written off")
            if issue > part.held + receive - write_off:
                raise FulfilmentError(f"«{name}», {part.name}: only {part.held + receive - write_off} can be issued")
    if row.mode == "parts":
        return
    if request.assemble > row.can_assemble:
        raise FulfilmentError(f"«{name}»: only {row.can_assemble} can be assembled")
    if request.receive > row.can_receive:
        raise FulfilmentError(f"«{name}»: only {row.can_receive} can be received")
    on_shelf = row.held + request.assemble + request.receive
    if request.write_off > on_shelf:
        raise FulfilmentError(f"«{name}»: only {on_shelf} can be written off")
    if request.issue > on_shelf - request.write_off:
        raise FulfilmentError(f"«{name}»: only {on_shelf - request.write_off} can be issued")


def _issued_after(row: LineState, request: LineRequest | None) -> bool:
    """Would ``row`` be fully issued once ``request`` is done?"""
    if row.mode == "parts":
        asked = request.parts if request is not None else {}
        return all(part.issued + asked.get(part.part_id, (0, 0))[1] >= part.wanted for part in row.parts)
    asked = request.parts if request is not None else {}
    return row.issued + (request.issue if request is not None else 0) >= row.ordered and all(
        part.issued + asked.get(part.part_id, (0, 0))[1] >= part.wanted for part in row.parts
    )


def _stocked_after(row: LineState, request: LineRequest | None) -> bool:
    """Would everything ``row`` ordered be on the shelf or issued once ``request`` is done (rule 36)?"""
    if request is None:
        return row.fully_stocked
    if row.mode == "parts":
        return all(
            part.issued
            + part.held
            + request.parts.get(part.part_id, (0, 0))[0]
            - request.parts_write_off.get(part.part_id, 0)
            >= part.wanted
            for part in row.parts
        )
    return row.issued + row.held + request.assemble + request.receive - request.write_off >= row.ordered and all(
        part.issued
        + part.held
        + request.parts.get(part.part_id, (0, 0))[0]
        - request.parts_write_off.get(part.part_id, 0)
        >= part.wanted
        for part in row.parts
    )


async def close(
    db: AsyncSession, project: Project, lines: Sequence[ProjectLine], *, to_stock: bool, actor: User | None
) -> None:
    """What completing does to the order's stock (spec workshop-order-issue, rule 12;
    followups, rule 37): kits nobody assembled go back on the shelf; an order closing to stock
    also puts everything it holds into free stock — the cancel's own doors, with the status
    «completed». The caller sets the status and journals it."""
    # The gates and the order row before any position (WS-13 E1 BL0 / BL3) — already
    # held when the caller took them at its start (the PATCH door, the issue dialog),
    # and then no statement at all.
    await product_gate(db, {line.product_id for line in lines})
    await lock_order(db, project.id)
    from backend.app.services.order_plan_submission import fence

    ordered = sorted(lines, key=lambda line: line.id)
    # Positions, lines, parts — before the first line is handled (BL0).
    await finished_stock.lock_lines_with_parts(db, ordered)
    # Publication owns the line's FK KEY SHARE before its draft. Fencing
    # before the stock/line set would hold the draft while waiting on that
    # same publisher's line and create a PostgreSQL deadlock.
    await fence(db, project.id)
    if to_stock:
        # The PATCH door read the state before these locks (final review M4): a write-off or
        # a close that committed meanwhile may have taken what the check counted.
        if not (await state(db, project, to_stock=True)).can_complete:
            raise FulfilmentError("Receive everything the order needs before closing it to stock")
        product_ids = {line.product_id for line in ordered}
        names = dict((await db.execute(select(Product.id, Product.name).where(Product.id.in_(product_ids)))).all())
        created_by = actor.id if actor is not None else None
        for line in ordered:
            # Rule 37: ``returned += held`` for every line — one that only took ready units too,
            # or the completed order would lose its coverage and could be reactivated (M5).
            units = await finished_stock.give_back_for_line(db, line, actor=actor, keep_history=True)
            parts = await part_stock.return_parts_for_line(db, line, created_by=created_by)
            if units:
                payload = {"line_id": line.id, "product": names.get(line.product_id), "units": units}
                await order_journal.record(db, project.id, "goods_stocked", payload, actor=actor)
            if parts:
                part_names = dict(
                    (await db.execute(select(ProductPart.id, ProductPart.name).where(ProductPart.id.in_(parts)))).all()
                )
                payload = {
                    "line_id": line.id,
                    "product": names.get(line.product_id),
                    "parts": [[part_names.get(pid), n] for pid, n in sorted(parts.items())],
                }
                await order_journal.record(db, project.id, "goods_stocked", payload, actor=actor)
    for line in ordered:
        await part_stock.release_for_line(db, line, note=part_stock.NOTE_ORDER_COMPLETED)


async def stocked_line_ids(db: AsyncSession, lines: Sequence[ProjectLine]) -> set[int]:
    """Lines whose goods went back to free stock — a cancel or a close to stock (followups, rule 41)."""
    out = {line.id for line in lines if line.mode != "parts" and (line.returned or 0) > 0}
    counters = await part_stock.line_part_stock(
        db, [line.id for line in lines if line.mode == "parts" or line.extra_percentages]
    )
    out |= {line_id for line_id, rows in counters.items() if any(row.returned > 0 for row in rows.values())}
    return out


async def apply(
    db: AsyncSession,
    project: Project,
    requests: Sequence[LineRequest],
    *,
    recipient: Recipient,
    waybill: str | None,
    note: str | None,
    complete: bool,
    actor: User | None,
    write_off_note: str | None = None,
) -> StockIssue | None:
    """Perform one batch in the caller's transaction; the issue it opened, or None."""
    if project.status != "active":
        raise FulfilmentError("Only an active order can be fulfilled")
    # Read, not the relationship: a caller may hold the order without its lines loaded.
    lines = {
        line.id: line
        for line in (await db.execute(select(ProjectLine).where(ProjectLine.project_id == project.id))).scalars()
    }
    asked: dict[int, LineRequest] = {}
    for request in requests:
        if request.line_id not in lines:
            raise FulfilmentError("Order line not found", 404)
        if request.line_id in asked:
            raise FulfilmentError("A line is named twice", 422)
        if any(n < 0 for n in request.numbers()):
            raise FulfilmentError("Quantity must be at least 0", 422)
        asked[request.line_id] = request
    if not complete and not any(n > 0 for request in asked.values() for n in request.numbers()):
        raise FulfilmentError("Nothing to do", 422)
    # A write-off says why (spec workshop-order-issue-followups, rule 46).
    writing_off = any(r.write_off or any(r.parts_write_off.values()) for r in asked.values())
    if writing_off and not (write_off_note or "").strip():
        raise FulfilmentError("A write-off needs a note", 422)

    # The products' gates (an assembly or a receipt may create a position — WS-13 E1
    # BL3), the order row, then positions in ascending id, then the lines — the order
    # every closing door takes (WS-10 M4; BL0) — and only then the numbers, read fresh
    # under the locks. The row before positions because completing ends in its UPDATE.
    await product_gate(db, {line.product_id for line in lines.values()})
    await lock_order(db, project.id)
    if set(await db.scalars(select(ProjectLine.product_id).where(ProjectLine.project_id == project.id))) - {
        line.product_id for line in lines.values()
    }:
        # A line of another product joined the order after the read above (BL2).
        raise FulfilmentError(ORDER_CHANGED, 409)
    await finished_stock.lock_lines_with_parts(db, lines.values())
    # The order as it stands behind its lock (BL2, implementation review V03): the status —
    # a cancel that committed while this request waited has given the shelf back (final
    # review M1) — and the customer the issue is written to and the mode is judged by.
    seen_customer = project.customer_id
    await db.refresh(project, ["status", "customer_id", "contact_id"])
    if project.status != "active":
        raise FulfilmentError("Only an active order can be fulfilled")
    if project.customer_id != seen_customer:
        # The operator confirmed this batch for the customer the request read; an issue
        # silently written to another one — or a close to stock the order no longer asks
        # for — is refused whole: nothing moved, no document.
        raise FulfilmentError(ORDER_CHANGED, 409)
    current = {row.line_id: row for row in (await state(db, project)).lines}
    for line_id in sorted(asked):
        _check(current[line_id], asked[line_id])
    to_stock = project.customer_id is None
    if complete:
        done = _stocked_after if to_stock else _issued_after
        if not all(done(row, asked.get(row.line_id)) for row in current.values()):
            raise FulfilmentError(
                "Receive everything the order needs before closing it to stock"
                if to_stock
                else "Issue everything the order holds before completing it"
            )
    issuing = sum(request.issue + sum(i for _r, i in request.parts.values()) for request in asked.values())
    if issuing and project.customer_id is None:
        raise FulfilmentError("An issue names its customer — set the order's customer first")

    created_by = actor.id if actor is not None else None
    for line_id in sorted(asked):
        line, request, name = lines[line_id], asked[line_id], current[line_id].product_name
        if request.assemble:
            await finished_stock.assemble_for_line(db, line, request.assemble, actor=actor)
            await order_journal.record(
                db,
                project.id,
                "kits_assembled",
                {"line_id": line_id, "product": name, "units": request.assemble},
                actor=actor,
            )
        if request.receive:
            await finished_stock.produce_for_line(db, line, request.receive, actor=actor)
            await order_journal.record(
                db,
                project.id,
                "goods_received",
                {"line_id": line_id, "product": name, "units": request.receive},
                actor=actor,
            )
        received = {pid: r for pid, (r, _i) in request.parts.items() if r > 0}
        if received:
            stock = {
                part.part_id: min(received.get(part.part_id, 0), part.stock_qty) for part in current[line_id].parts
            }
            await part_stock.receive_parts_for_line(db, line, received, created_by=created_by, stock_counts=stock)
            names = {part.part_id: part.name for part in current[line_id].parts}
            await order_journal.record(
                db,
                project.id,
                "goods_received",
                {
                    "line_id": line_id,
                    "product": name,
                    "parts": [[names[pid], n] for pid, n in sorted(received.items())],
                },
                actor=actor,
            )
        if request.write_off:
            await finished_stock.write_off_from_line(db, line, request.write_off, note=write_off_note, actor=actor)
            await order_journal.record(
                db,
                project.id,
                "goods_written_off",
                {"line_id": line_id, "product": name, "units": request.write_off, "note": write_off_note.strip()},
                actor=actor,
            )
        written_off = {pid: n for pid, n in request.parts_write_off.items() if n > 0}
        if written_off:
            await part_stock.write_off_parts_for_line(db, line, written_off, note=write_off_note, created_by=created_by)
            names = {part.part_id: part.name for part in current[line_id].parts}
            await order_journal.record(
                db,
                project.id,
                "goods_written_off",
                {
                    "line_id": line_id,
                    "product": name,
                    "parts": [[names[pid], n] for pid, n in sorted(written_off.items())],
                    "note": write_off_note.strip(),
                },
                actor=actor,
            )

    issue = None
    if issuing:
        issue = await stock_issues.create(
            db,
            customer_id=project.customer_id,
            project_id=project.id,
            recipient=recipient,
            waybill=waybill,
            note=note,
            actor=actor,
        )
        for line_id in sorted(asked):
            line, request = lines[line_id], asked[line_id]
            if request.issue:
                await finished_stock.issue_from_line(db, line, request.issue, stock_issue=issue, actor=actor)
            given = {pid: i for pid, (_r, i) in request.parts.items() if i > 0}
            if given:
                await part_stock.issue_parts_for_line(db, line, given, stock_issue_id=issue.id, created_by=created_by)
        await stock_issues.seal(db, issue, actor=actor)
        await order_journal.record(
            db,
            project.id,
            "goods_issued",
            {"issue_id": issue.id, "units": issuing, "waybill": issue.waybill},
            actor=actor,
        )

    if complete:
        project.status = "completed"
        # Everything ordered went out — or, without a customer, onto free stock; kits nobody
        # assembled go back on the shelf (spec rule 12; followups, rule 37).
        await close(db, project, list(lines.values()), to_stock=to_stock, actor=actor)
        await order_journal.record(db, project.id, "status_changed", {"from": "active", "to": "completed"}, actor=actor)
    await db.flush()
    return issue
