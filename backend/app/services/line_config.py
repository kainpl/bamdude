"""An order line's configuration — the ONE writer (spec workshop-product-variants, rules 5, 11–14).

Writes ``project_line_choices``, ``project_line_part_counts`` and
``project_lines.config_key`` and nothing else does
(``tests/unit/test_line_config_has_one_writer.py``). Every reader asks
``services/line_composition.py``.

* A ``product`` line records a choice for EVERY group of its product — the
  standard one too — so a changed standard or a group added later never
  changes a saved order's kit. A changed count is stored only when it differs
  from what the configuration would give anyway.
* A ``parts`` line records the count of each part it wants, and no choice.
* Changing a configuration moves the line's reservation to the new kit in the
  same transaction and journals ``line_configured``; ``dry_run`` answers what it
  would do — the parts that drop out, what of them is already printed or queued
  — and writes nothing.

Never commits; SQLite runs no FK actions, so a part or line that goes takes its
rows with it through :func:`forget_part` / :func:`forget_line`. Rows are
replaced with Core statements, never ORM objects (a reader in the same session
may hold the old ones in its identity map).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.core.lock_ledger import ledger
from backend.app.models.finished_stock import StockItem, StockItemChoice, StockItemPartCount
from backend.app.models.line_config import ProjectLineChoice, ProjectLinePartCount
from backend.app.models.product import Product, ProductPart
from backend.app.models.product_variant import ProductVariantGroup
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.app.models.user import User
from backend.app.services import order_journal, part_stock
from backend.app.services.line_composition import (
    Composition,
    LineConfig,
    composition,
    config_key,
    counted,
    default_options,
    in_kit,
    line_composition,
    load_item_configs,
    load_line_configs,
    snapshot_extra_percentages,
    standard_per,
)

MAX_COUNT = 9999


class LineConfigError(ValueError):
    """A configuration the product cannot have; ``status`` is the HTTP answer."""

    def __init__(self, detail: str, status: int = 422) -> None:
        super().__init__(detail)
        self.status = status


@dataclass
class DroppedPart:
    """A part the new configuration wants less of — and what of it already exists."""

    part_id: int
    name: str
    per_before: int
    per_after: int
    printed: int
    queued: int


@dataclass
class ConfigOutcome:
    reserved_before: int = 0
    reserved_after: int = 0
    dropping: list[DroppedPart] = field(default_factory=list)
    #: The configuration key the line has (or, on a dry run, would have) after the change.
    new_key: str = ""


async def _product(db: AsyncSession, product_id: int) -> Product:
    product = (
        await db.execute(
            select(Product)
            .options(
                selectinload(Product.parts),
                selectinload(Product.variant_groups).selectinload(ProductVariantGroup.options),
            )
            .where(Product.id == product_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return product


async def load_products(db: AsyncSession, product_ids: Iterable[int]) -> dict[int, Product]:
    """Products with their parts and groups (with options) — one statement for any number."""
    ids = sorted(set(product_ids))
    if not ids:
        return {}
    rows = await db.execute(
        select(Product)
        .options(
            selectinload(Product.parts),
            selectinload(Product.variant_groups).selectinload(ProductVariantGroup.options),
        )
        .where(Product.id.in_(ids))
        .execution_options(populate_existing=True)
    )
    return {product.id: product for product in rows.scalars()}


def resolve_on(
    product: Product, choices: Mapping[int, int], counts: Mapping[int, int] | None = None
) -> tuple[str, dict[int, int], dict[int, int]]:
    """:func:`resolve` over a loaded product — the key and the choices and counts to store; pure."""
    new_choices, new_counts = _validate(product, "product", choices, counts or {})
    return config_key("product", new_choices, new_counts), new_choices, new_counts


def _defaults(product: Product) -> dict[int, int]:
    return {g.id: g.default_option_id for g in product.variant_groups if g.default_option_id is not None}


def _validate(
    product: Product, mode: str, choices: Mapping[int, int], counts: Mapping[int, int]
) -> tuple[dict[int, int], dict[int, int]]:
    """The choices and counts to store, or :class:`LineConfigError`."""
    parts = {p.id: p for p in product.parts}
    for pid, qty in counts.items():
        if pid not in parts:
            raise LineConfigError("That part does not belong to this product")
        if not 0 <= qty <= MAX_COUNT:
            raise LineConfigError("A part count must be between 0 and 9999")
        if qty > 0 and parts[pid].ignored:
            # spec workshop-order-issue-followups, rule 34: «не рахувати» is not a part.
            raise LineConfigError("That part is not counted")
    if mode == "parts":
        wanted = {pid: qty for pid, qty in counts.items() if qty > 0}
        if not wanted:
            raise LineConfigError("A parts line needs at least one part")
        return {}, wanted
    group_of = {o.id: g.id for g in product.variant_groups for o in g.options}
    new_choices = _defaults(product)
    for gid, oid in choices.items():
        if group_of.get(oid) != gid:
            raise LineConfigError("That option does not belong to this product")
        new_choices[gid] = oid
    # A count equal to what the configuration gives anyway is not a change.
    by_id = {p.id: per for p, per in composition(list(parts.values()), "product", set(new_choices.values()), {})}
    changed = {pid: qty for pid, qty in counts.items() if qty != by_id.get(pid, 0)}
    return new_choices, changed


async def _write_rows(
    db: AsyncSession,
    choice_model,
    count_model,
    owner: str,
    owner_id: int,
    choices: Mapping[int, int],
    counts: Mapping[int, int],
) -> None:
    """Replace one holder's rows — a line's or a stock position's."""
    await db.execute(delete(choice_model).where(getattr(choice_model, owner) == owner_id))
    await db.execute(delete(count_model).where(getattr(count_model, owner) == owner_id))
    if choices:
        await db.execute(
            insert(choice_model),
            [{owner: owner_id, "group_id": gid, "option_id": oid} for gid, oid in sorted(choices.items())],
        )
    if counts:
        await db.execute(
            insert(count_model),
            [{owner: owner_id, "part_id": pid, "qty": qty} for pid, qty in sorted(counts.items())],
        )


async def _write(db: AsyncSession, line: ProjectLine, choices: Mapping[int, int], counts: Mapping[int, int]) -> None:
    await _write_rows(db, ProjectLineChoice, ProjectLinePartCount, "line_id", line.id, choices, counts)
    line.config_key = config_key(line.mode, choices, counts)


_SAME_POSITION = "That change would make two stock positions the same configuration"


async def _apply_item_configs(db: AsyncSession, product_id: int, new: Mapping[int, LineConfig]) -> None:
    """Write new configurations for some of a product's stock positions.

    Checked BEFORE the first write: two positions of one product may never end
    up with the same key (spec workshop-finished-goods, rule 14) — that would
    silently merge two shelves. The keys move through a temporary value so an
    intermediate state never trips the unique index.
    """
    if not new:
        return
    items = {
        item.id: item
        for item in (await db.execute(select(StockItem).where(StockItem.product_id == product_id))).scalars()
    }
    keys = {item_id: item.config_key for item_id, item in items.items()}
    for item_id, cfg in new.items():
        keys[item_id] = config_key("product", cfg.choices, cfg.counts)
    if len(set(keys.values())) != len(keys):
        raise LineConfigError(_SAME_POSITION, 409)
    for item_id, cfg in new.items():
        await _write_rows(db, StockItemChoice, StockItemPartCount, "item_id", item_id, cfg.choices, cfg.counts)
        items[item_id].config_key = f"#rekey:{item_id}"
    await db.flush()
    for item_id in new:
        items[item_id].config_key = keys[item_id]
    await db.flush()


async def _item_configs(db: AsyncSession, product_id: int) -> dict[int, LineConfig]:
    """Every stock position of a product with its configuration."""
    ids = (await db.execute(select(StockItem.id).where(StockItem.product_id == product_id))).scalars().all()
    return await load_item_configs(db, ids) if ids else {}


async def resolve(
    db: AsyncSession, product_id: int, choices: Mapping[int, int], counts: Mapping[int, int] | None = None
) -> tuple[str, dict[int, int], dict[int, int]]:
    """The key a stock position of this configuration has, and the choices and
    counts to store — validated exactly as an order line's; writes nothing."""
    return resolve_on(await _product(db, product_id), choices, counts)


async def seed_item(
    db: AsyncSession, item: StockItem, choices: Mapping[int, int], counts: Mapping[int, int] | None = None
) -> None:
    """A new stock position's configuration rows (spec workshop-finished-goods, rule 9)."""
    product = await _product(db, item.product_id)
    new_choices, new_counts = _validate(product, "product", choices, counts or {})
    await _write_rows(db, StockItemChoice, StockItemPartCount, "item_id", item.id, new_choices, new_counts)
    item.config_key = config_key("product", new_choices, new_counts)


async def forget_items(db: AsyncSession, item_ids: Sequence[int]) -> None:
    """Deleted stock positions take their configuration with them (SQLite runs no FK actions)."""
    if not item_ids:
        return
    await db.execute(delete(StockItemChoice).where(StockItemChoice.item_id.in_(item_ids)))
    await db.execute(delete(StockItemPartCount).where(StockItemPartCount.item_id.in_(item_ids)))


async def _rekey(db: AsyncSession, line_ids: Sequence[int]) -> None:
    """Recompute the stored key of these lines from their rows."""
    if not line_ids:
        return
    configs = await load_line_configs(db, line_ids)
    for line in (await db.execute(select(ProjectLine).where(ProjectLine.id.in_(line_ids)))).scalars():
        cfg = configs.get(line.id, LineConfig())
        line.config_key = config_key(line.mode, cfg.choices, cfg.counts)


def _describe(product: Product, cfg: LineConfig) -> dict:
    """Names only — the journal composes the sentence in the reader's language."""
    options = {o.id: o.name for g in product.variant_groups for o in g.options}
    parts = {p.id: p.name for p in product.parts}
    return {
        "choices": [
            [g.name, options.get(cfg.choices[g.id], "?")] for g in product.variant_groups if g.id in cfg.choices
        ],
        "changed": [[parts.get(pid, "?"), qty] for pid, qty in sorted(cfg.counts.items())],
    }


async def _dropping(
    db: AsyncSession, line: ProjectLine, product: Product, old: Composition, new: Composition
) -> list[DroppedPart]:
    """Every counted part the new kit wants less of, with how many of its
    printed and of its queued would BECOME surplus — the printed and queued
    counts come from the order's own figures and plan; only the difference in
    surplus is worked out here, against the full quantity as the figures
    measure it (``order_metrics._finish``). A count that drops from 4 to 3 on
    a line whose prints are still short turns nothing into surplus."""
    from backend.app.services.order_metrics import attribute, load_order_context
    from backend.app.services.plan_engine import queued_yield_by_line
    from backend.app.services.product_composition import recipes_for_products

    new_per = {p.id: per for p, per in new}
    shrinking = [(p, per, new_per.get(p.id, 0)) for p, per in counted(old) if new_per.get(p.id, 0) < per]
    if not shrinking:
        return []
    ctx = await load_order_context(db, line.project_id)
    usable: dict[int, int] = {}
    if ctx is not None:
        figures, _other = attribute(ctx)
        if line.id in figures:
            usable = {pf.part_id: pf.usable for pf in figures[line.id].parts}
    recipes = await recipes_for_products(db, [product])
    queued = await queued_yield_by_line(db, recipes, [line], {line.id: {p.id for p, _per in counted(old)}})
    line_queued = queued.get(line.id, {})
    units = 1 if line.mode == "parts" else line.quantity

    def becomes_surplus(total: int, before: int, after: int) -> int:
        return max(0, total - after * units) - max(0, total - before * units)

    out: list[DroppedPart] = []
    for part, before, after in shrinking:
        printed, queued = usable.get(part.id, 0), line_queued.get(part.id, 0)
        from_printed = becomes_surplus(printed, before, after)
        out.append(
            DroppedPart(
                part_id=part.id,
                name=part.name,
                per_before=before,
                per_after=after,
                printed=from_printed,
                queued=becomes_surplus(printed + queued, before, after) - from_printed,
            )
        )
    return out


async def seed_line(
    db: AsyncSession,
    line: ProjectLine,
    *,
    choices: Mapping[int, int] | None,
    counts: Mapping[int, int] | None,
    sliced_memo: dict[int, set[int]] | None = None,
) -> None:
    """A new line's configuration: the given choices, the standard for every other group.

    Every line constructor passes here, so it is also where the lock ledger learns
    the line is this transaction's own new row (WS-13 E1 BL0: nobody can wait on it),
    and where a ``product`` line's combination is checked (spec
    product-part-variant-combinations, п. 13): a closed one is
    ``combination_gate.CombinationUnavailable``, before anything is written.
    ``sliced_memo`` — a caller adding several lines in one call keeps the products'
    sliced sets there, read once per product (``combination_gate.ensure_available``)."""
    ledger(db).note_created("project_lines", line.id)
    product = await _product(db, line.product_id)
    new_choices, new_counts = _validate(product, line.mode, choices or {}, counts or {})
    if line.mode == "product":
        # Imported here: the gate's refusal is a LineConfigError, so it imports this module.
        from backend.app.services import combination_gate

        await combination_gate.ensure_available(db, product, new_choices, sliced_memo=sliced_memo)
    await _write(db, line, new_choices, new_counts)

    line.extra_percentages = snapshot_extra_percentages(product.parts) if line.mode == "product" else {}


async def target_key(
    db: AsyncSession, line: ProjectLine, *, choices: Mapping[int, int], counts: Mapping[int, int]
) -> str:
    """The key ``set_configuration`` would give ``line`` — read only, so the door can
    lock the new configuration's position with the old one's, before the line (WS-13
    E1 BL0). Refuses exactly as ``set_configuration`` would."""
    product = await _product(db, line.product_id)
    current = (await load_line_configs(db, [line.id])).get(line.id, LineConfig())
    new_choices, new_counts = _validate(product, line.mode, {**current.choices, **choices}, counts)
    return config_key(line.mode, new_choices, new_counts)


async def set_configuration(
    db: AsyncSession,
    line: ProjectLine,
    *,
    choices: Mapping[int, int],
    counts: Mapping[int, int],
    actor: User | None,
    dry_run: bool = False,
) -> ConfigOutcome:
    """Change a line's configuration (spec rules 11, 13–14).

    ``choices`` names the groups to change — the others keep the line's current
    choice; ``counts`` is the WHOLE set of changed counts (or, for a parts line,
    of wanted counts): what is not in it goes back to the standard.

    A changed CHOICE must be an available combination — checked before the dry run,
    so a preview of a closed one is refused too; counts alone are never checked
    (spec product-part-variant-combinations, п. 11, 13). "Changed" is judged against
    the saved choices merged over the product's standards — the choices the line's kit
    reads — so a line saved without a row for a group that took a standard later is
    not checked on a change of its counts.
    """
    status = await db.scalar(select(Project.status).where(Project.id == line.project_id))
    if status == "completed":
        # Its kits shipped (Ruling 25): the ledger still holds them as taken, and
        # moving the reservation would put shipped parts back on the shelf.
        # Reopen the order to change what it was.
        raise LineConfigError("A completed order's lines cannot be reconfigured", 409)
    product = await _product(db, line.product_id)
    current = (await load_line_configs(db, [line.id])).get(line.id, LineConfig())
    new_choices, new_counts = _validate(product, line.mode, {**current.choices, **choices}, counts)
    if line.mode == "product" and new_choices != {**_defaults(product), **current.choices}:
        from backend.app.services import combination_gate

        await combination_gate.ensure_available(db, product, new_choices)
    reserved_before = await part_stock.reserved_units_for_line(db, line)
    reserved_parts = await part_stock.reserved_parts_for_line(db, line)
    if new_choices == current.choices and new_counts == current.counts:
        # Nothing changes: no rows, no reservation move, no journal entry.
        return ConfigOutcome(reserved_before, reserved_before, [], line.config_key)
    parts = list(product.parts)
    old_comp = line_composition(parts, line.mode, current, _defaults(product))
    new_comp = composition(parts, line.mode, set(new_choices.values()), new_counts)
    if dry_run:
        dropping = await _dropping(db, line, product, old_comp, new_comp)
        after = 0
        if reserved_parts:
            shelf = await part_stock.balances(db, product.id)
            # The line's own kits come back first — the rewrite releases them.
            for part_id, held in reserved_parts.items():
                shelf[part_id] = shelf.get(part_id, 0) + held
            after = min(reserved_before, line.quantity, part_stock.kits_of(shelf, new_comp))
        return ConfigOutcome(reserved_before, after, dropping, config_key(line.mode, new_choices, new_counts))
    from backend.app.services.order_recount import ensure_attribution_can_change

    await ensure_attribution_can_change(db, line.project_id, [line.product_id], configuration=True)
    await _write(db, line, new_choices, new_counts)
    await db.flush()
    after = 0
    if reserved_parts:
        after = await part_stock.reserve_for_line(
            db, line, reserved_before, comp=new_comp, created_by=actor.id if actor else None
        )
    await order_journal.record(
        db,
        line.project_id,
        "line_configured",
        {
            "line_id": line.id,
            "product": product.name,
            "from": _describe(product, current),
            "to": _describe(product, LineConfig(new_choices, new_counts)),
            "reserved": [reserved_before, after],
        },
        actor=actor,
    )
    return ConfigOutcome(reserved_before, after, [], line.config_key)


def _per_under(part: ProductPart, bound: Mapping[int, frozenset[int]], chosen: set[int]) -> int:
    """The part's standard count on a line that chose ``chosen``, were it bound to ``bound``."""
    return standard_per(part) if in_kit(bound, chosen) else 0


async def freeze_binding(db: AsyncSession, part: ProductPart, new_bound: Mapping[int, frozenset[int]]) -> int:
    """A part's binding is about to change: every saved product line whose kit
    that would change keeps it, through an explicit count row (the principle
    rule 5 applies to a new group or a new standard — a saved order's kit does
    not move under it). Lines created afterwards follow the new binding.
    ``new_bound`` is the ``group_id → option ids`` about to be written over
    ``part.bound``; a group with no options in it is no condition («any»), never
    «none», and is dropped. Returns the number of lines and stock positions frozen."""
    current = part.bound
    new_bound = {group_id: frozenset(options) for group_id, options in new_bound.items() if options}
    if current == new_bound:
        return 0
    defaults = (await default_options(db, [part.product_id])).get(part.product_id, {})
    frozen_items: dict[int, LineConfig] = {}
    for item_id, cfg in (await _item_configs(db, part.product_id)).items():
        if part.id in cfg.counts:
            continue
        chosen = set({**defaults, **cfg.choices}.values())
        before = _per_under(part, current, chosen)
        if before != _per_under(part, new_bound, chosen):
            frozen_items[item_id] = LineConfig(dict(cfg.choices), {**cfg.counts, part.id: before})
    await _apply_item_configs(db, part.product_id, frozen_items)
    line_ids = (
        (
            await db.execute(
                select(ProjectLine.id).where(ProjectLine.product_id == part.product_id, ProjectLine.mode == "product")
            )
        )
        .scalars()
        .all()
    )
    if not line_ids:
        return len(frozen_items)
    configs = await load_line_configs(db, line_ids)
    rows = []
    for line_id in line_ids:
        cfg = configs.get(line_id, LineConfig())
        if part.id in cfg.counts:
            continue  # the line already says how many it wants
        chosen = set({**defaults, **cfg.choices}.values())
        before = _per_under(part, current, chosen)
        if before != _per_under(part, new_bound, chosen):
            rows.append({"line_id": line_id, "part_id": part.id, "qty": before})
    if rows:
        await db.execute(insert(ProjectLinePartCount), rows)
        await _rekey(db, [r["line_id"] for r in rows])
    return len(rows) + len(frozen_items)


async def add_group_to_lines(db: AsyncSession, group: ProductVariantGroup) -> int:
    """A group added to a product already on orders: every product line of it
    records the group's standard option (rule 5 — choices are explicit)."""
    if group.default_option_id is None:
        return 0
    await _apply_item_configs(
        db,
        group.product_id,
        {
            item_id: LineConfig({**cfg.choices, group.id: group.default_option_id}, dict(cfg.counts))
            for item_id, cfg in (await _item_configs(db, group.product_id)).items()
            if group.id not in cfg.choices
        },
    )
    line_ids = (
        (
            await db.execute(
                select(ProjectLine.id).where(ProjectLine.product_id == group.product_id, ProjectLine.mode == "product")
            )
        )
        .scalars()
        .all()
    )
    if not line_ids:
        return 0
    have = set(
        (
            await db.execute(
                select(ProjectLineChoice.line_id).where(
                    ProjectLineChoice.line_id.in_(line_ids), ProjectLineChoice.group_id == group.id
                )
            )
        )
        .scalars()
        .all()
    )
    missing = [lid for lid in line_ids if lid not in have]
    if missing:
        await db.execute(
            insert(ProjectLineChoice),
            [{"line_id": lid, "group_id": group.id, "option_id": group.default_option_id} for lid in missing],
        )
    await _rekey(db, line_ids)
    return len(missing)


async def forget_part(db: AsyncSession, part_id: int) -> None:
    """A deleted part takes its count rows with it — of lines and of stock positions
    (a position whose key would then match another's refuses the deletion: 409)."""
    product_id = await db.scalar(select(ProductPart.product_id).where(ProductPart.id == part_id))
    if product_id is not None:
        await _apply_item_configs(
            db,
            product_id,
            {
                item_id: LineConfig(dict(cfg.choices), {pid: n for pid, n in cfg.counts.items() if pid != part_id})
                for item_id, cfg in (await _item_configs(db, product_id)).items()
                if part_id in cfg.counts
            },
        )
    line_ids = (
        (await db.execute(select(ProjectLinePartCount.line_id).where(ProjectLinePartCount.part_id == part_id)))
        .scalars()
        .all()
    )
    await db.execute(delete(ProjectLinePartCount).where(ProjectLinePartCount.part_id == part_id))
    await _rekey(db, list(line_ids))


async def repoint_part(db: AsyncSession, source_id: int, target_id: int) -> None:
    """A merge says the source IS the target. A line that changed either one
    wants, of the merged part, what it wanted of both — each read as the line
    had it (its row, else its standard under its choices) — stored only where
    that differs from the target's standard; a line that changed neither
    follows the product (the target's own count). Call before the source goes."""
    parts = {
        p.id: p
        for p in (await db.execute(select(ProductPart).where(ProductPart.id.in_([source_id, target_id])))).scalars()
    }
    source, target = parts.get(source_id), parts.get(target_id)
    if source is None or target is None:
        return
    defaults = (await default_options(db, [target.product_id])).get(target.product_id, {})
    merged_items: dict[int, LineConfig] = {}
    for item_id, cfg in (await _item_configs(db, target.product_id)).items():
        if source_id not in cfg.counts and target_id not in cfg.counts:
            continue
        chosen = set({**defaults, **cfg.choices}.values())
        standard = _per_under(target, target.bound, chosen)
        merged = min(
            MAX_COUNT,
            cfg.counts.get(target_id, standard) + cfg.counts.get(source_id, _per_under(source, source.bound, chosen)),
        )
        counts = {pid: n for pid, n in cfg.counts.items() if pid not in (source_id, target_id)}
        if merged != standard:
            counts[target_id] = merged
        merged_items[item_id] = LineConfig(dict(cfg.choices), counts)
    await _apply_item_configs(db, target.product_id, merged_items)
    line_ids = sorted(
        set(
            (
                await db.execute(
                    select(ProjectLinePartCount.line_id).where(ProjectLinePartCount.part_id.in_([source_id, target_id]))
                )
            )
            .scalars()
            .all()
        )
    )
    if not line_ids:
        return
    configs = await load_line_configs(db, line_ids)
    modes = dict((await db.execute(select(ProjectLine.id, ProjectLine.mode).where(ProjectLine.id.in_(line_ids)))).all())
    await db.execute(
        delete(ProjectLinePartCount).where(
            ProjectLinePartCount.line_id.in_(line_ids), ProjectLinePartCount.part_id.in_([source_id, target_id])
        )
    )
    rows = []
    for line_id in line_ids:
        cfg = configs.get(line_id, LineConfig())
        if modes.get(line_id) == "parts":
            merged, standard = cfg.counts.get(target_id, 0) + cfg.counts.get(source_id, 0), 0
        else:
            chosen = set({**defaults, **cfg.choices}.values())
            standard = _per_under(target, target.bound, chosen)
            merged = cfg.counts.get(target_id, standard) + cfg.counts.get(
                source_id, _per_under(source, source.bound, chosen)
            )
        merged = min(MAX_COUNT, merged)
        if merged != standard:
            rows.append({"line_id": line_id, "part_id": target_id, "qty": merged})
    if rows:
        await db.execute(insert(ProjectLinePartCount), rows)
    await _rekey(db, line_ids)


async def part_in_use(db: AsyncSession, part_id: int) -> bool:
    """Does an order line or a stock position want this part — a stored count above zero?
    (spec workshop-order-issue-followups, rule 34: such a part cannot be marked «не рахувати».)"""
    for model in (ProjectLinePartCount, StockItemPartCount):
        found = await db.scalar(select(model.part_id).where(model.part_id == part_id, model.qty > 0).limit(1))
        if found is not None:
            return True
    return False


async def forget_line(db: AsyncSession, line_id: int) -> None:
    """A deleted line takes its configuration with it."""
    await db.execute(delete(ProjectLineChoice).where(ProjectLineChoice.line_id == line_id))
    await db.execute(delete(ProjectLinePartCount).where(ProjectLinePartCount.line_id == line_id))


async def copy_configuration(db: AsyncSession, source: ProjectLine, target: ProjectLine) -> None:
    """The copy of an order carries each line's configuration."""
    ledger(db).note_created("project_lines", target.id)
    cfg = (await load_line_configs(db, [source.id])).get(source.id, LineConfig())
    target.mode = source.mode
    target.extra_percentages = dict(source.extra_percentages or {})
    await _write(db, target, cfg.choices, cfg.counts)


async def ensure_no_extra_snapshot(db: AsyncSession, product_id: int, part_ids: Sequence[int]) -> None:
    """Catalog edits of a snapshotted extra must not silently change an order's obligation."""
    ids = {str(pid) for pid in part_ids}
    rows = await db.execute(select(ProjectLine.extra_percentages).where(ProjectLine.product_id == product_id))
    if any(any((snapshot or {}).get(pid, 0) for pid in ids) for snapshot in rows.scalars()):
        raise LineConfigError("This part has additional quantities in an order; keep it as a separate part", 409)
    rows = await db.scalars(select(ProjectLine.part_count_adjustments).where(ProjectLine.product_id == product_id))
    if any(any((snapshot or {}).get(pid, 0) for pid in ids) for snapshot in rows):
        raise LineConfigError(
            "This part has order recount corrections; reverse the corrections before changing it", 409
        )
