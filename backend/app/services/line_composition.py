"""A line's composition — the ONE reader (spec workshop-product-variants, rules 8–10, 12).

Every figure that asks "which parts, how many per unit, for THIS line" asks
here: the order figures, the reservation, the stock tab, the filing of prints.
The product's own ``qty_per_unit`` is only the STANDARD answer; a line may
choose options and change counts (``services/line_config.py`` writes that).
Outside the product's authoring code and the stock ledger's own "has a shelf"
predicate, nothing reads ``qty_per_unit`` —
``tests/unit/test_line_composition_is_the_one_reader.py`` scans for it.
"""

from collections.abc import Iterable, Mapping, Sequence, Set as AbstractSet
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.finished_stock import StockItemChoice, StockItemPartCount
from backend.app.models.line_config import ProjectLineChoice, ProjectLinePartCount
from backend.app.models.product import ProductPart
from backend.app.models.product_variant import ProductVariantGroup

Composition = list[tuple[ProductPart, int]]
# Large enough for a page of lines, small enough for asyncpg's bind-parameter limit.
_CHUNK = 500


def snapshot_extra_percentages(parts: Iterable[ProductPart]) -> dict[str, float]:
    """New product lines inherit current printed-part defaults, never live settings."""
    return {
        str(part.id): float(part.extra_percent)
        for part in parts
        if part.kind == "printed" and (part.extra_percent or 0) > 0
    }


def extra_parts_for_line(line, comp: Composition) -> dict[int, int]:
    """Round upward once over the WHOLE line, not per unit or per print batch.

    Decimal(str(...)) uses the stored decimal spelling: a 10% extra of ten
    parts is one part, without a binary float turning it into two. Parts-mode
    lines already specify exact counts and never acquire implicit extras.
    """
    if line.mode == "parts":
        return {}
    percents = line.extra_percentages or {}
    return {
        part.id: int(
            (Decimal(line.quantity) * per * Decimal(str(percents.get(str(part.id), 0))) / 100).to_integral_value(
                rounding=ROUND_CEILING
            )
        )
        for part, per in counted(comp)
    }


@dataclass
class LineConfig:
    """What a line chose — ``group_id → option_id`` — and its changed counts, ``part_id → qty``."""

    choices: dict[int, int] = field(default_factory=dict)
    counts: dict[int, int] = field(default_factory=dict)


def in_kit(bound: Mapping[int, frozenset[int]], chosen: set[int]) -> bool:
    """Is a part bound to ``bound`` (``group_id → option ids``) in the kit of a line that
    chose ``chosen``? Yes when every group it has options in has one of them chosen —
    AND across groups, OR within one; an unbound part (``{}``) always is."""
    return all(not options.isdisjoint(chosen) for options in bound.values())


def composition(parts: Sequence[ProductPart], mode: str, chosen: set[int], counts: Mapping[int, int]) -> Composition:
    """The line's parts with their per-unit count; in ``parts`` mode, the wanted count.

    ``product`` mode: every part :func:`in_kit` for the chosen options, at its
    standard count — unless the line changed it; a count of 0 drops the part,
    and a count on a part outside the kit brings it in.
    """
    ordered = sorted(parts, key=lambda p: (p.sort_order or 0, p.id))
    if mode == "parts":
        return [(p, counts[p.id]) for p in ordered if counts.get(p.id, 0) > 0]
    out: Composition = []
    for p in ordered:
        if p.id in counts:
            per = counts[p.id]
        elif in_kit(p.bound, chosen):
            per = p.qty_per_unit
        else:
            continue
        if per > 0:
            out.append((p, per))
    return out


def line_composition(
    parts: Sequence[ProductPart], mode: str, config: LineConfig, defaults: Mapping[int, int] | None = None
) -> Composition:
    """:func:`composition` for a loaded :class:`LineConfig`.

    ``defaults`` is the product's ``group_id → standard option``: a group the
    line has no choice for reads its standard option. The writer records a
    choice for every group, so this only covers the moment between a group's
    creation and its backfill — and a hand-built context in a test.
    """
    chosen = {**(defaults or {}), **config.choices}
    return composition(parts, mode, set(chosen.values()), config.counts)


def snapshot_parts(comp: Composition) -> list[dict]:
    return [
        {
            "id": getattr(part, "snapshot_catalog_id", part.id),
            "identity": part.composition_identity,
            "name": part.name,
            "kind": part.kind,
            "qty": qty,
            "ignored": bool(part.ignored),
            "variant": is_variant_part(part),
        }
        for part, qty in comp
    ]


def saved_composition(line, catalog: Sequence[ProductPart], config: LineConfig, defaults=None) -> Composition:
    """Read the document, falling back only for rows without a captured revision."""
    if not line.composition_revisions:
        return line_composition(catalog, line.mode, config, defaults)
    return composition_from_snapshot(line.product_id, line.composition_revisions[-1]["parts"], catalog)


def composition_from_snapshot(product_id: int, snapshot: Sequence[dict], catalog: Sequence[ProductPart]) -> Composition:
    by_id = {part.id: part for part in catalog}
    result = []
    used = set(by_id)
    next_archived = -max((abs(row["id"]) for row in snapshot), default=0) - 1
    for row in snapshot:
        catalog_id = row["id"]
        live = by_id.get(catalog_id)
        if live is not None and live.composition_identity != row["identity"]:
            live = None
        part_id = catalog_id
        if live is None and (row["kind"] == "purchased" or part_id in used):
            part_id = -abs(catalog_id)
            if part_id in used:
                part_id = next_archived
                next_archived -= 1
        used.add(part_id)
        # Keep the document's name and membership while allowing established
        # readers to use current sourcing metadata for an extant identity.
        part = ProductPart(
            id=part_id,
            composition_identity=row["identity"],
            product_id=product_id,
            name=row["name"],
            kind=row["kind"],
            ignored=row["ignored"],
            unit_price=live.unit_price if live is not None else None,
            sourcing_url=live.sourcing_url if live is not None else None,
        )
        part.snapshot_variant = row["variant"]
        part.snapshot_catalog_id = catalog_id
        result.append((part, row["qty"]))
    return result


def is_variant_part(part: ProductPart) -> bool:
    captured = getattr(part, "snapshot_variant", None)
    return captured if captured is not None else bool(part.bound)


def part_identity(part: ProductPart) -> str:
    return part.composition_identity or str(part.id)


def kit_signature(parts: Sequence[dict]) -> tuple[tuple[str, int], ...]:
    return tuple(sorted((row["identity"] or str(row["id"]), row["qty"]) for row in parts))


def stock_composition(item, parts: Sequence[ProductPart], config: LineConfig, defaults=None) -> Composition:
    if item.composition_parts is not None:
        return composition_from_snapshot(item.product_id, item.composition_parts, parts)
    return line_composition(parts, "product", config, defaults)


def standard_composition(parts: Sequence[ProductPart], default_option_ids: set[int]) -> Composition:
    """The product's own kit: the standard option of every group, standard counts."""
    return composition(parts, "product", default_option_ids, {})


def has_shelf(part: ProductPart) -> bool:
    """Does this part have a stock balance at all — printed, and not marked «не рахувати»
    (spec workshop-order-issue-followups, rule 34). A zero in the product's kit does NOT take
    the shelf away: that part is out of the kit and still a part. The stock ledger's
    definition (``part_stock.is_counted`` delegates here), kept beside the reader so the
    figures can ask it without importing the ledger."""
    return part.kind == "printed" and not part.ignored


def counted(comp: Iterable[tuple[ProductPart, int]]) -> Composition:
    """The parts that have a shelf and make kits: printed, wanted at all."""
    return [(p, per) for p, per in comp if p.kind == "printed" and per > 0]


def config_key(mode: str, choices: Mapping[int, int], counts: Mapping[int, int]) -> str:
    """Stable identity of a configuration — the finished-goods position of WS-09.

    The same choices and the same changed counts give the same key; a product
    without groups and without changes is ``''``.
    """
    counts_part = ";".join(f"{pid}={counts[pid]}" for pid in sorted(counts))
    if mode == "parts":
        return f"parts:{counts_part}"
    choices_part = ";".join(f"{gid}={choices[gid]}" for gid in sorted(choices))
    return f"{choices_part}|{counts_part}" if counts else choices_part


async def _load_configs(
    db: AsyncSession, choice_model, count_model, owner: str, ids: Sequence[int]
) -> dict[int, LineConfig]:
    """Choices and counts of a set of configuration holders — two statements per chunk."""
    ids = list(dict.fromkeys(ids))
    out: dict[int, LineConfig] = {oid: LineConfig() for oid in ids}
    for start in range(0, len(ids), _CHUNK):
        chunk = ids[start : start + _CHUNK]
        choice_owner, count_owner = getattr(choice_model, owner), getattr(count_model, owner)
        for owner_id, group_id, option_id in (
            await db.execute(
                select(choice_owner, choice_model.group_id, choice_model.option_id).where(choice_owner.in_(chunk))
            )
        ).all():
            out[owner_id].choices[group_id] = option_id
        for owner_id, part_id, qty in (
            await db.execute(select(count_owner, count_model.part_id, count_model.qty).where(count_owner.in_(chunk)))
        ).all():
            out[owner_id].counts[part_id] = qty
    return out


async def load_line_configs(db: AsyncSession, line_ids: Sequence[int]) -> dict[int, LineConfig]:
    """Every line's choices and counts — two statements per chunk, whatever the page size."""
    return await _load_configs(db, ProjectLineChoice, ProjectLinePartCount, "line_id", line_ids)


async def load_item_configs(db: AsyncSession, item_ids: Sequence[int]) -> dict[int, LineConfig]:
    """Every stock position's choices and counts (spec workshop-finished-goods, rule 2) —
    the same shape as a line's; a position is always a ``product`` configuration."""
    return await _load_configs(db, StockItemChoice, StockItemPartCount, "item_id", item_ids)


async def default_options(db: AsyncSession, product_ids: Iterable[int]) -> dict[int, dict[int, int]]:
    """``product_id → {group_id → standard option id}`` — one statement per chunk."""
    ids = list(dict.fromkeys(product_ids))
    out: dict[int, dict[int, int]] = {pid: {} for pid in ids}
    for start in range(0, len(ids), _CHUNK):
        for product_id, group_id, option_id in (
            await db.execute(
                select(
                    ProductVariantGroup.product_id, ProductVariantGroup.id, ProductVariantGroup.default_option_id
                ).where(
                    ProductVariantGroup.product_id.in_(ids[start : start + _CHUNK]),
                    ProductVariantGroup.default_option_id.is_not(None),
                )
            )
        ).all():
            out[product_id][group_id] = option_id
    return out


async def compositions_for_lines(
    db: AsyncSession, lines: Sequence, parts_by_product: Mapping[int, Sequence[ProductPart]]
) -> dict[int, Composition]:
    """``line_id → composition`` for many lines — three statements, whatever the count."""
    configs = await load_line_configs(db, [line.id for line in lines])
    defaults = await default_options(db, {line.product_id for line in lines})
    return {
        line.id: saved_composition(
            line,
            parts_by_product.get(line.product_id, []),
            configs.get(line.id, LineConfig()),
            defaults.get(line.product_id, {}),
        )
        for line in lines
    }


async def compositions_for_items(
    db: AsyncSession, items: Sequence, parts_by_product: Mapping[int, Sequence[ProductPart]]
) -> dict[int, Composition]:
    """``item_id → composition`` for many stock positions — three statements, whatever the count."""
    configs = await load_item_configs(db, [item.id for item in items])
    defaults = await default_options(db, {item.product_id for item in items})
    return {
        item.id: stock_composition(
            item,
            parts_by_product.get(item.product_id, []),
            configs.get(item.id, LineConfig()),
            defaults.get(item.product_id, {}),
        )
        for item in items
    }


def per_by_line(compositions: Mapping[int, Composition]) -> dict[int, dict[int, int]]:
    """``line_id → {part_id → per}`` over the counted parts — the divisor the
    stock ledger reads a line's reservation back through."""
    return {line_id: {p.id: per for p, per in counted(comp)} for line_id, comp in compositions.items()}


def standard_per(part: ProductPart) -> int:
    """A part's per-unit count in the product's own kit — the named door for a
    reader that shows the product's standard (the stock shelf's row)."""
    return part.qty_per_unit


@dataclass(frozen=True)
class Unavailable:
    """Why a combination of choices cannot be ordered (spec product-part-variant-combinations,
    п. 10): ``option_without_part`` names the group and the chosen option no part of the
    kit names; ``part_not_sliced`` names a printed part of the kit without a sliced source."""

    code: str
    group_id: int | None
    option_id: int | None
    part_id: int | None


def availability(
    parts: Sequence[ProductPart],
    choices: Mapping[int, int],
    sliced: AbstractSet[int],
    strict_groups: AbstractSet[int],
) -> list[Unavailable]:
    """Is the combination ``choices`` (``group_id → option_id``, every group) one an order may
    take? ``[]`` — yes; otherwise the reasons, groups in id order, then the parts in kit
    order (п. 10–11).

    Judged on the STANDARD kit of the choice — a line's changed counts never open a closed
    combination. Only parts with a standard count and without «не рахувати» count.

    ``option_without_part`` is asked only in a group marked «every value needs its own
    part» (``strict_groups``: ``ProductVariantGroup.requires_part``) that some counted part
    has options in: there the chosen option must be named by a part of the kit. An
    unmarked group never closes anything by this rule — «Magnet: no / yes» with the magnet
    bound to «yes» keeps «no» orderable. When a chosen value is named by NO counted part at
    all (or nothing is chosen), such values are reported, and of the other groups only a
    value that stays unnamed with those groups' conditions taken as met — the rest merely
    have no kit left around them.

    ``part_not_sliced``, for every product with variant groups: a printed part of the kit
    needs a sliced source (``sliced``: part ids); a purchased one needs none. Pure —
    ``combination_gate`` reads ``sliced`` and the marked groups and asks here."""
    chosen = set(choices.values())
    ordered = sorted(parts, key=lambda p: (p.sort_order or 0, p.id))
    # ``bound`` is rebuilt on every access: read once per part, per call.
    counted_parts = [(p, p.bound) for p in ordered if not p.ignored and standard_per(p) > 0]
    kit = [(p, bound) for p, bound in counted_parts if in_kit(bound, chosen)]
    named: dict[int, set[int]] = {}
    for _p, bound in counted_parts:
        for group_id, options in bound.items():
            named.setdefault(group_id, set()).update(options)
    missing: list[Unavailable] = []
    for group_id in sorted(set(named) & set(strict_groups)):
        option_id = choices.get(group_id)
        if not any(option_id in bound.get(group_id, ()) for _p, bound in kit):
            missing.append(Unavailable("option_without_part", group_id, option_id, None))
    nowhere = [u for u in missing if u.option_id not in named[u.group_id]]
    out = missing
    if nowhere:
        # A value no part names empties the kit around it, so the other groups' values
        # look unnamed too. Ask again with those groups' conditions taken as met: what is
        # still unnamed is a reason of its own and is reported beside them.
        absent = {u.group_id for u in nowhere}
        around = [
            bound
            for _p, bound in counted_parts
            if in_kit({g: options for g, options in bound.items() if g not in absent}, chosen)
        ]
        own = [
            u
            for u in missing
            if u.group_id not in absent and not any(u.option_id in bound.get(u.group_id, ()) for bound in around)
        ]
        out = sorted(nowhere + own, key=lambda u: u.group_id or 0)
    out += [
        Unavailable("part_not_sliced", None, None, p.id) for p, _b in kit if p.kind == "printed" and p.id not in sliced
    ]
    return out
