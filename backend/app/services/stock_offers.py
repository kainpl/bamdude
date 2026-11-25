"""«Взяти зі складу» — ready units and kits for what an order has not printed, is not
printing and has not queued (spec workshop-order-issue, rule 17).

The offer is the plan's own arithmetic — ``plan_engine.unplanned_units`` over the figures
and the queue reading the plan endpoint uses — handed to ``stock_pick.suggest``, the one
function that answers what the shelves give; asked WITHOUT a line id, because an offer
ADDS to the line and the units it already holds are not free for it. Taking goes through
the two add-only doors (``finished_stock.take_for_line``, ``part_stock.add_kits_for_line``)
— never a release first, whatever the line has moved — and what it gets may be less than
what was shown: the shelf is clamped, never refused (a reservation, as in WS-10).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.product import ProductPart
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.app.models.user import User
from backend.app.services import finished_stock, order_journal, part_stock, plan_engine, stock_pick
from backend.app.services.line_composition import counted, load_line_configs
from backend.app.services.order_metrics import LineFigures, OrderContext, attribute, composition_of, load_order_context
from backend.app.services.product_composition import recipes_for_products


class StockOfferError(Exception):
    """A take the order refuses; ``status`` is the HTTP answer."""

    def __init__(self, detail: str, status: int = 409) -> None:
        super().__init__(detail)
        self.status = status


@dataclass(frozen=True, slots=True)
class Offer:
    line_id: int
    product_name: str
    from_finished: int
    kits: int
    parts: dict[int, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Taken:
    line_id: int
    asked_finished: int
    got_finished: int
    asked_kits: int
    got_kits: int
    asked_parts: dict[int, int] = field(default_factory=dict)
    got_parts: dict[int, int] = field(default_factory=dict)


async def offers(db: AsyncSession, project: Project) -> list[Offer]:
    """What each product line of an ACTIVE order could take off the shelves now — lines
    with nothing to take are left out."""
    if project.status != "active":
        return []
    ctx = await load_order_context(db, project.id)
    if ctx is None:
        return []
    figures, _other = attribute(ctx)
    lines = [line for line in ctx.lines if line.mode == "product"]
    if not lines:
        return []
    recipes = await recipes_for_products(db, ctx.products_by_id.values())
    queued = await plan_engine.queued_yield_by_line(
        db, recipes, ctx.lines, plan_engine.counted_parts_by_line({project.id: figures})
    )
    wants = _wants(ctx, figures, queued)
    if not wants:
        return []
    supply = await load_supply(db, replace(ctx, lines=[line for line in lines if line.id in wants]))
    return offers_for(ctx, figures, queued, supply)


@dataclass(frozen=True, slots=True)
class Supply:
    """The existing offer's shelf reads, reusable across read-only what-if quantities."""

    ready: dict[int, stock_pick.Suggestion]
    parts: dict[int, dict[int, int]]


async def load_supply(db: AsyncSession, ctx: OrderContext) -> Supply:
    lines = [line for line in ctx.lines if line.mode == "product"]
    configs = await load_line_configs(db, [line.id for line in lines])
    suggestions = await stock_pick.suggest(
        db,
        [
            stock_pick.PickRequest(
                product_id=line.product_id,
                choices=configs[line.id].choices if line.id in configs else {},
                counts=configs[line.id].counts if line.id in configs else {},
                # The capacity fields are independent of requested quantity.
                quantity=1,
                saved_kit=composition_of(ctx, line),
            )
            for line in lines
        ],
    )
    return Supply(
        dict(zip((line.id for line in lines), suggestions, strict=True)),
        await part_stock.balances_for_products(db, sorted({line.product_id for line in lines})),
    )


def _wants(
    ctx: OrderContext, figures: Mapping[int, LineFigures], queued: Mapping[int, dict[int, int]]
) -> dict[int, int]:
    lines = [line for line in ctx.lines if line.mode == "product"]
    wants: dict[int, int] = {}
    for line in lines:
        figs = figures[line.id]
        want = min(
            plan_engine.unplanned_units(figs, queued.get(line.id, {})), max(0, figs.quantity - figs.covered_units)
        )
        if want > 0 or any(
            p.extra_qty and p.remaining > p.in_progress + queued.get(line.id, {}).get(p.part_id, 0) for p in figs.parts
        ):
            wants[line.id] = want
    return wants


def offers_for(
    ctx: OrderContext,
    figures: Mapping[int, LineFigures],
    queued: Mapping[int, dict[int, int]],
    supply: Supply,
) -> list[Offer]:
    """The SAME greedy shared-shelf pass used by offers/take, without database writes.

    The input shelves are copied: one quantity's virtual take must not reduce
    the next quantity's shelf. Earlier order lines still get first claim.
    """
    lines = [line for line in ctx.lines if line.mode == "product"]
    wants = _wants(ctx, figures, queued)
    asked = [line for line in lines if line.id in wants]
    shelves = {pid: dict(parts) for pid, parts in supply.parts.items()}
    suggestions = [supply.ready[line.id] for line in asked]
    # Suggestions describe each row's shelf. Offers are one batch: spend a
    # shared shelf once, including two configurations of the same product.
    ready_left: dict[int, int] = {}
    fitted_suggestions = []
    for line, suggestion in zip(asked, suggestions, strict=True):
        shelf = shelves.get(line.product_id, {})
        ready = 0
        if suggestion.position_id is not None:
            available = ready_left.setdefault(suggestion.position_id, suggestion.finished_free)
            ready = min(wants[line.id], available)
            ready_left[suggestion.position_id] -= ready
        kit = counted(composition_of(ctx, line))
        held = {pf.part_id: pf.allocated_qty for pf in figures[line.id].parts}
        # Extras contribute to remaining demand but cannot fill the base-kit
        # writer's room. Use its same bound before spending the shared shelf.
        kits = min(
            wants[line.id] - ready,
            part_stock.kits_of(shelf, kit),
            max(0, part_stock.kit_reservation_room(line, kit, held) - ready),
        )
        # A partial reservation can already cover one component completely.
        # A whole kit is an offer only if EVERY component is still needed;
        # otherwise the loose-part pass takes just the missing components.
        kits = (
            min(
                kits,
                *(
                    max(0, pf.remaining - pf.in_progress - queued.get(line.id, {}).get(pf.part_id, 0) - ready * pf.per)
                    // pf.per
                    for pf in figures[line.id].parts
                    if pf.per > 0 and pf.shelf
                ),
            )
            if kit
            else 0
        )
        for part, per in kit:
            shelf[part.id] -= kits * per
        fitted_suggestions.append(replace(suggestion, from_finished=ready, from_kits=kits))
    out = []
    for line, suggestion in zip(asked, fitted_suggestions, strict=True):
        shelf = shelves.get(line.product_id, {})
        loose = {}
        for pf in figures[line.id].parts:
            if pf.per <= 0 or not pf.shelf:
                continue
            # Complete kit offers share the same shelf with loose parts and
            # later lines. A finished unit comes from a different ledger.
            need = max(
                0,
                pf.remaining
                - pf.in_progress
                - queued.get(line.id, {}).get(pf.part_id, 0)
                - (suggestion.from_finished + suggestion.from_kits) * pf.per,
            )
            take = min(need, shelf[pf.part_id])
            if take:
                loose[pf.part_id] = take
                shelf[pf.part_id] -= take
        if suggestion.from_finished or suggestion.from_kits or loose:
            product = ctx.products_by_id.get(line.product_id)
            out.append(
                Offer(
                    line_id=line.id,
                    product_name=product.name if product is not None else "",
                    from_finished=suggestion.from_finished,
                    kits=suggestion.from_kits,
                    parts=loose,
                )
            )
    return out


async def take(
    db: AsyncSession,
    project: Project,
    shown: Mapping[int, tuple[int, int] | tuple[int, int, dict[int, int]]] | None = None,
    *,
    actor: User | None,
) -> list[Taken]:
    """Take the offers — the numbers the operator was SHOWN (``line_id → (ready, kits)``),
    or the current offers when nothing was shown — no more than the offer is now, and
    what the add doors can actually take. Journals ``stock_taken`` per line that got any."""
    if project.status != "active":
        raise StockOfferError("Only an active order takes finished goods from stock")
    lines = {
        line.id: line
        for line in (
            await db.execute(
                select(ProjectLine).where(ProjectLine.project_id == project.id, ProjectLine.mode == "product")
            )
        ).scalars()
    }
    # Positions in ascending id, then the lines, then their parts (WS-13 E1 BL0) — then the
    # offers, read fresh under the locks.
    await finished_stock.lock_lines_with_parts(db, lines.values())
    if await db.scalar(select(Project.status).where(Project.id == project.id)) != "active":
        raise StockOfferError("Only an active order takes finished goods from stock")
    current = {offer.line_id: offer for offer in await offers(db, project)}
    asked = (
        dict(shown) if shown is not None else {lid: (o.from_finished, o.kits, o.parts) for lid, o in current.items()}
    )
    from backend.app.services.order_recount import ensure_attribution_can_change

    changing = {
        lines[lid].product_id
        for lid, values in asked.items()
        if lid in lines and (values[0] or values[1] or (len(values) > 2 and any(values[2].values())))
    }
    await ensure_attribution_can_change(db, project.id, changing)
    out = []
    for line_id in sorted(asked):
        line = lines.get(line_id)
        if line is None:
            continue
        asked_finished, asked_kits, *parts = asked[line_id]
        asked_parts = parts[0] if parts else {}
        offer = current.get(line_id)
        finished = min(asked_finished, offer.from_finished) if offer is not None else 0
        kits = min(asked_kits, offer.kits) if offer is not None else 0
        got_finished = await finished_stock.take_for_line(db, line, finished, actor=actor) if finished else 0
        got_kits = (
            await part_stock.add_kits_for_line(db, line, kits, created_by=actor.id if actor is not None else None)
            if kits
            else 0
        )
        wanted_parts = {pid: min(qty, offer.parts.get(pid, 0)) for pid, qty in asked_parts.items()} if offer else {}
        got_parts = (
            await part_stock.add_parts_for_line(
                db, line, wanted_parts, created_by=actor.id if actor is not None else None
            )
            if wanted_parts
            else {}
        )
        if got_finished or got_kits or got_parts:
            part_names = (
                dict(
                    (
                        await db.execute(select(ProductPart.id, ProductPart.name).where(ProductPart.id.in_(got_parts)))
                    ).all()
                )
                if got_parts
                else {}
            )
            await order_journal.record(
                db,
                project.id,
                "stock_taken",
                {
                    "line_id": line_id,
                    "product": offer.product_name if offer is not None else None,
                    "from_finished": got_finished,
                    "kits": got_kits,
                    **(
                        {
                            "parts": got_parts,
                            "parts_taken": [[part_names[pid], qty] for pid, qty in sorted(got_parts.items())],
                        }
                        if got_parts
                        else {}
                    ),
                },
                actor=actor,
            )
        out.append(Taken(line_id, asked_finished, got_finished, asked_kits, got_kits, asked_parts, got_parts))
    await db.flush()
    return out
