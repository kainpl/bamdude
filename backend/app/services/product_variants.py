"""Variant groups and options — the ONE writer (WS-13 E1, spec VR1) — and of the parts'
bindings to them.

Only this module writes ``product_variant_groups`` / ``product_variant_options``
(``tests/unit/test_product_variants_have_one_writer.py``): the six variant routes,
the atomic ``PUT /products/{id}/variants`` (``apply``), a product copy
(``copy_groups``) and the ZIP import (``create_group`` with ``record_lines=False``).

It is also the one writer of ``product_part_options`` — which options a part is bound
to, across groups (spec product-part-variant-combinations, rule 3;
``tests/unit/test_part_options_have_one_writer.py``): a part's own door
(``set_part_options`` — create, edit, the ZIP import) and a product copy
(``copy_part_options``). A part's deletion takes its rows through the ORM cascade.

Every write takes the product gate first (``product_gate``, spec BL1 / BL2) and
reads the groups fresh behind it; an edit that rewrites line and position
configurations — a new group records its standard on each of them — takes its
footprint with NOWAIT (BL5) and answers ``product_busy`` instead of waiting.

Refusals are :class:`VariantError` — the same English sentences and statuses the
routes always answered, forwarded verbatim (translated at the boundary). Never
commits.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, inspect as sqla_inspect, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.models.finished_stock import StockItemChoice
from backend.app.models.line_config import ProjectLineChoice
from backend.app.models.product import ProductPart
from backend.app.models.product_variant import (
    ProductPartOption,
    ProductVariantGroup,
    ProductVariantOption,
    variant_key,
)
from backend.app.services import line_config
from backend.app.services.product_gate import PRODUCT_BUSY, ProductBusy, lock_configurations, product_gate

GROUP_TAKEN = "A group with this name already exists"
OPTION_TAKEN = "An option with this name already exists"
FOREIGN_OPTION = "That option does not belong to this product"


class VariantError(Exception):
    """A refusal: ``detail`` is a sentence, or ``{"error": code, "message": sentence, …}``."""

    def __init__(self, status: int, detail: str | Mapping[str, Any]) -> None:
        super().__init__(detail if isinstance(detail, str) else detail.get("message", ""))
        self.status = status
        self.detail = detail


def busy() -> VariantError:
    return VariantError(409, {"error": "product_busy", "message": PRODUCT_BUSY})


async def groups(db: AsyncSession, product_id: int) -> list[ProductVariantGroup]:
    """The product's groups with their options, fresh — read behind the gate, never
    from an identity map another statement of this request filled."""
    return list(
        (
            await db.execute(
                select(ProductVariantGroup)
                .options(selectinload(ProductVariantGroup.options))
                .where(ProductVariantGroup.product_id == product_id)
                .order_by(ProductVariantGroup.position, ProductVariantGroup.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )


async def _group(db: AsyncSession, product_id: int, group_id: int) -> ProductVariantGroup:
    group = next((g for g in await groups(db, product_id) if g.id == group_id), None)
    if group is None:
        raise VariantError(404, "Variant group not found")
    return group


def _option(group: ProductVariantGroup, option_id: int) -> ProductVariantOption:
    option = next((o for o in group.options if o.id == option_id), None)
    if option is None:
        raise VariantError(404, "Variant option not found")
    return option


async def _bound_parts(db: AsyncSession, option_ids: Sequence[int]) -> int:
    """How many DISTINCT parts are bound to any of these options — a part bound to two
    options of one group is one part (the «N parts» counter and the delete guards)."""
    if not option_ids:
        return 0
    return (
        await db.scalar(
            select(func.count(func.distinct(ProductPartOption.part_id))).where(
                ProductPartOption.option_id.in_(option_ids)
            )
        )
        or 0
    )


# ---------- a part's bindings (spec product-part-variant-combinations, rules 3, 4, 8) ----------


async def set_part_options(db: AsyncSession, part: ProductPart, option_ids: Sequence[int]) -> None:
    """Bind ``part`` to exactly ``option_ids`` — options of any of its product's groups;
    an empty list puts it in every configuration (rule 4).

    An option that is not one of the product's own is a 422 and nothing changes. A part
    already in the database freezes first (``line_config.freeze_binding``, rule 8): every
    saved line and stock position whose kit the new binding would change keeps its old
    count — a ``LineConfigError`` (409) when two positions would become one
    configuration, raised before anything is written. A part not yet in the database is
    new: no saved line knows it, so it enters the final composition with nothing frozen
    (WS-13 E10 A05, K22). A persisted part must come from a query — its ``option_links``
    loaded — and this is called BEFORE the links change, or the freeze compares the new
    binding with itself.

    Never commits; the caller holds the product gate (spec rule 20)."""
    new = not sqla_inspect(part).has_identity  # transient or pending: never flushed
    wanted = list(dict.fromkeys(option_ids))
    group_of: dict[int, int] = {}
    if wanted:
        # A pending part must not be flushed by this read before its links are set.
        with db.no_autoflush:
            group_of = dict(
                (
                    await db.execute(
                        select(ProductVariantOption.id, ProductVariantOption.group_id)
                        .join(ProductVariantGroup, ProductVariantGroup.id == ProductVariantOption.group_id)
                        .where(ProductVariantOption.id.in_(wanted), ProductVariantGroup.product_id == part.product_id)
                    )
                ).all()
            )
        if len(group_of) != len(wanted):
            raise VariantError(422, FOREIGN_OPTION)
    new_bound: dict[int, set[int]] = {}
    for option_id in wanted:
        new_bound.setdefault(group_of[option_id], set()).add(option_id)
    if not new:
        await line_config.freeze_binding(
            db, part, {group_id: frozenset(options) for group_id, options in new_bound.items()}
        )
    kept = [link for link in part.option_links if link.option_id in group_of]
    have = {link.option_id for link in kept}
    added = [
        ProductPartOption(option_id=option_id, group_id=group_of[option_id])
        for option_id in wanted
        if option_id not in have
    ]
    if added or len(kept) != len(part.option_links):
        part.option_links = kept + added


def copy_part_options(
    source: ProductPart, target: ProductPart, option_map: Mapping[int, int], group_map: Mapping[int, int]
) -> None:
    """A product copy's part takes the source part's bindings, through the copy's own
    options and groups (``copy_groups``' maps). The target is new — nothing to freeze."""
    target.option_links = [
        ProductPartOption(option_id=option_map[link.option_id], group_id=group_map[link.group_id])
        for link in source.option_links
        if link.option_id in option_map and link.group_id in group_map
    ]


async def _gate(db: AsyncSession, product_id: int, *, footprint: bool = False) -> None:
    await product_gate(db, [product_id])
    if footprint:
        try:
            await lock_configurations(db, product_id)
        except ProductBusy as e:
            raise busy() from e


async def create_group(
    db: AsyncSession,
    product_id: int,
    name: str,
    options: Sequence[str],
    *,
    default_index: int = 0,
    record_lines: bool = True,
    requires_part: bool = False,
) -> ProductVariantGroup:
    """A group with its options, ``options[default_index]`` standard. With
    ``record_lines`` (every door but a brand-new product's import) the standard is
    recorded on each line and position of the product (rule 5). ``requires_part`` —
    «every value needs its own part» (spec product-part-variant-combinations, п. 10)."""
    await _gate(db, product_id, footprint=record_lines)
    existing = await groups(db, product_id)
    if not options:
        raise VariantError(422, "A group needs at least one option")
    if any(variant_key(g.name) == variant_key(name) for g in existing):
        raise VariantError(409, GROUP_TAKEN)
    keys = [variant_key(n) for n in options]
    if len(set(keys)) != len(keys):
        raise VariantError(409, OPTION_TAKEN)
    group = await _insert_group(
        db,
        product_id,
        name,
        options,
        default_index=default_index,
        position=max((g.position for g in existing), default=-1) + 1,
        requires_part=requires_part,
    )
    if record_lines:
        await line_config.add_group_to_lines(db, group)
    return group


async def _insert_group(
    db: AsyncSession,
    product_id: int,
    name: str,
    options: Sequence[str],
    *,
    default_index: int,
    position: int,
    requires_part: bool = False,
) -> ProductVariantGroup:
    group = ProductVariantGroup(product_id=product_id, name=name, position=position, requires_part=requires_part)
    db.add(group)
    await db.flush()
    created = [ProductVariantOption(group_id=group.id, name=n, position=i) for i, n in enumerate(options)]
    db.add_all(created)
    await db.flush()
    group.default_option_id = created[default_index].id
    await db.flush()
    await db.refresh(group, ["options"])
    return group


async def update_group(db: AsyncSession, product_id: int, group_id: int, fields: Mapping[str, Any]) -> None:
    """Rename, reorder, pick another standard, or mark «every value needs its own part».
    A new standard changes no saved line — every line recorded its choice (rule 5); the
    mark changes no line or position either — it decides only what a new line may take."""
    await _gate(db, product_id)
    group = await _group(db, product_id, group_id)
    if "name" in fields and variant_key(fields["name"]) != variant_key(group.name):
        if any(
            g.id != group.id and variant_key(g.name) == variant_key(fields["name"])
            for g in await groups(db, product_id)
        ):
            raise VariantError(409, GROUP_TAKEN)
    if "default_option_id" in fields and fields["default_option_id"] not in {o.id for o in group.options}:
        raise VariantError(422, "That option does not belong to this group")
    for column in ("name", "default_option_id", "position", "requires_part"):
        if column in fields:
            setattr(group, column, fields[column])
    await db.flush()


async def delete_group(db: AsyncSession, product_id: int, group_id: int) -> None:
    """Refused while an order line or a stock position has a choice in it, or a part
    is bound to one of its options — each would change a kit somebody relies on."""
    await _gate(db, product_id)
    group = await _group(db, product_id, group_id)
    lines, held, bound = await _group_counts(db, group)
    if lines:
        raise VariantError(409, f"Group chosen in {lines} order lines")
    if held:
        raise VariantError(409, f"Group held by {held} stock positions")
    if bound:
        raise VariantError(409, f"{bound} parts are bound to this group's options")
    # The group points at one of its options: clear that first, so the options
    # can go before the group on a backend that enforces the key.
    group.default_option_id = None
    await db.flush()
    await db.delete(group)
    await db.flush()


async def add_option(db: AsyncSession, product_id: int, group_id: int, name: str) -> None:
    await _gate(db, product_id)
    group = await _group(db, product_id, group_id)
    if any(variant_key(o.name) == variant_key(name) for o in group.options):
        raise VariantError(409, OPTION_TAKEN)
    group.options.append(
        ProductVariantOption(name=name, position=max((o.position for o in group.options), default=-1) + 1)
    )
    await db.flush()


async def update_option(
    db: AsyncSession, product_id: int, group_id: int, option_id: int, fields: Mapping[str, Any]
) -> None:
    await _gate(db, product_id)
    group = await _group(db, product_id, group_id)
    option = _option(group, option_id)
    if "name" in fields and any(
        o.id != option.id and variant_key(o.name) == variant_key(fields["name"]) for o in group.options
    ):
        raise VariantError(409, OPTION_TAKEN)
    for column in ("name", "position"):
        if column in fields:
            setattr(option, column, fields[column])
    await db.flush()


async def delete_option(db: AsyncSession, product_id: int, group_id: int, option_id: int) -> None:
    """Refused for the standard option, for one an order line chose, one a stock
    position holds and one parts are bound to (spec workshop-product-variants)."""
    await _gate(db, product_id)
    group = await _group(db, product_id, group_id)
    option = _option(group, option_id)
    if option.id == group.default_option_id:
        raise VariantError(409, "The standard option cannot be deleted; choose another standard first")
    lines, held, bound = await _option_counts(db, option.id)
    if lines:
        raise VariantError(409, f"Option chosen in {lines} order lines")
    if held:
        raise VariantError(409, f"Option held by {held} stock positions")
    if bound:
        raise VariantError(409, f"{bound} parts are bound to this option")
    group.options.remove(option)
    await db.flush()


async def copy_groups(db: AsyncSession, source_id: int, target_id: int) -> tuple[dict[int, int], dict[int, int]]:
    """A product copy's groups (the target is new — nothing records on it yet).
    Returns ``old option id → new option id`` and ``old group id → new group id`` for
    the parts' bindings (``copy_part_options``)."""
    option_map: dict[int, int] = {}
    group_map: dict[int, int] = {}
    for group in await groups(db, source_id):
        new_group = ProductVariantGroup(
            product_id=target_id, name=group.name, position=group.position, requires_part=group.requires_part
        )
        db.add(new_group)
        await db.flush()
        group_map[group.id] = new_group.id
        new_options = [
            ProductVariantOption(group_id=new_group.id, name=o.name, position=o.position) for o in group.options
        ]
        db.add_all(new_options)
        await db.flush()
        option_map.update({o.id: n.id for o, n in zip(group.options, new_options, strict=True)})
        new_group.default_option_id = option_map.get(group.default_option_id) if group.default_option_id else None
    await db.flush()
    return option_map, group_map


# ---------- the atomic draft (spec VR4–VR8) ----------

VARIANTS_CHANGED = "The variants changed since they were opened — reload and try again"
OPTION_MOVED = "An existing option cannot move to another group"
DRAFT_REF = "Every group and option of the draft names either its id or a temp_id"
DRAFT_TWICE = "An id or temp_id appears twice in the draft"
DRAFT_DEFAULT = "The standard must be one of the group's own options"
NO_OPTIONS = "A group needs at least one option"


@dataclass
class OptionDraft:
    id: int | None
    temp_id: str | None
    name: str


@dataclass
class GroupDraft:
    id: int | None
    temp_id: str | None
    name: str
    options: list[OptionDraft]
    #: ``None`` only for an existing group whose stored standard is already ``None``
    #: (WS-13 E10 A06) — checked against the stored group in :func:`apply`.
    default: int | str | None
    #: «Every value needs its own part»; ``None`` — not sent: an existing group keeps its
    #: stored mark (a client that does not know the field never clears it), a new one is off.
    requires_part: bool | None = None


def revision(state: Sequence[Any]) -> str:
    """SHA-256 of the product's variants as the client saw them (spec VR4): groups by
    id with name, position, standard and «every value needs its own part»; options by id
    with name and position. The order rows were loaded in never changes it; ``position``
    is a field, not the order."""
    canonical = [
        {
            "id": g.id,
            "name": g.name,
            "position": g.position,
            "default_option_id": g.default_option_id,
            "requires_part": bool(g.requires_part),
            "options": [
                {"id": o.id, "name": o.name, "position": o.position} for o in sorted(g.options, key=lambda o: o.id)
            ],
        }
        for g in sorted(state, key=lambda g: g.id)
    ]
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _refusal(status: int, code: str, message: str, *, group=None, option=None) -> VariantError:
    return VariantError(status, {"error": code, "message": message, "group": group, "option": option})


def _ref(item) -> int | str | None:
    return item.id if item.id is not None else item.temp_id


def _check_form(draft: Sequence[GroupDraft]) -> None:
    """Step 1 of VR5 — the draft alone, no database: one reference per element, no
    reference twice, options in every group, a standard among its own options."""
    seen_ids: set[tuple[str, int]] = set()
    seen_temp: set[str] = set()

    def claim(kind: str, item) -> None:
        if (item.id is None) == (item.temp_id is None):
            raise _refusal(422, "invalid_draft", DRAFT_REF, **{kind: _ref(item)})
        if item.id is not None:
            if (kind, item.id) in seen_ids:
                raise _refusal(422, "invalid_draft", DRAFT_TWICE, **{kind: item.id})
            seen_ids.add((kind, item.id))
        else:
            if item.temp_id in seen_temp:
                raise _refusal(422, "invalid_draft", DRAFT_TWICE, **{kind: item.temp_id})
            seen_temp.add(item.temp_id)

    for group in draft:
        claim("group", group)
        # An EXISTING group may arrive empty or without a standard: whether it already
        # was is the stored group's question, answered in ``apply`` (WS-13 E10 A06).
        inherited = group.id is not None
        if not group.options and not inherited:
            raise _refusal(422, "invalid_draft", NO_OPTIONS, group=_ref(group))
        for option in group.options:
            claim("option", option)
        if group.default is None:
            if not inherited:
                raise _refusal(422, "invalid_draft", DRAFT_DEFAULT, group=_ref(group))
        elif group.default not in {_ref(o) for o in group.options}:
            raise _refusal(422, "invalid_draft", DRAFT_DEFAULT, group=_ref(group))


def _ordered(group: ProductVariantGroup) -> list[ProductVariantOption]:
    return sorted(group.options, key=lambda o: (o.position, o.id))


async def apply(db: AsyncSession, product_id: int, seen_revision: str, draft: Sequence[GroupDraft]) -> bool:
    """Make the product's variants equal ``draft`` in this transaction (spec VR5).
    Returns False when the draft changed nothing (and nothing was written, VR7)."""
    _check_form(draft)
    await _gate(db, product_id, footprint=any(group.id is None for group in draft))
    current = await groups(db, product_id)
    if revision(current) != seen_revision:
        raise _refusal(409, "variants_changed", VARIANTS_CHANGED)

    by_group = {g.id: g for g in current}
    owner = {o.id: g.id for g in current for o in g.options}
    options_by_id = {o.id: o for g in current for o in g.options}
    for group in draft:
        if group.id is not None and group.id not in by_group:
            raise VariantError(404, "Variant group not found")
        for option in group.options:
            if option.id is None:
                continue
            if option.id not in owner:
                raise VariantError(404, "Variant option not found")
            if group.id is None or owner[option.id] != group.id:
                raise _refusal(422, "option_moved", OPTION_MOVED, group=_ref(group), option=option.id)
    # The inherited state, two INDEPENDENT checks against the stored group (WS-13 E10
    # A06, R11): no standard only where the group already had none — its name and
    # options may still change, a standard never appears by itself — and no options
    # only where it was already empty. A standard is never taken away, a non-empty
    # group is never emptied.
    for group in draft:
        if group.id is None:
            continue
        stored = by_group[group.id]
        if group.default is None and stored.default_option_id is not None:
            raise _refusal(422, "invalid_draft", DRAFT_DEFAULT, group=group.id)
        if not group.options and stored.options:
            raise _refusal(422, "invalid_draft", NO_OPTIONS, group=group.id)
    group_keys: set[str] = set()
    for group in draft:
        if variant_key(group.name) in group_keys:
            raise _refusal(409, "group_name_taken", GROUP_TAKEN, group=_ref(group))
        group_keys.add(variant_key(group.name))
        option_keys: set[str] = set()
        for option in group.options:
            if variant_key(option.name) in option_keys:
                raise _refusal(409, "option_name_taken", OPTION_TAKEN, group=_ref(group), option=_ref(option))
            option_keys.add(variant_key(option.name))

    kept_groups = {g.id for g in draft if g.id is not None}
    kept_options = {o.id for g in draft for o in g.options if o.id is not None}
    dropped_groups = [g for g in current if g.id not in kept_groups]
    dropped_options = [o for g in current if g.id in kept_groups for o in g.options if o.id not in kept_options]
    # The delete guards — the single routes' own, read before a byte is written.
    for option in dropped_options:
        lines, held, bound = await _option_counts(db, option.id)
        where = {"group": owner[option.id], "option": option.id}
        if lines:
            raise _refusal(409, "option_in_use", f"Option chosen in {lines} order lines", **where)
        if held:
            raise _refusal(409, "option_in_use", f"Option held by {held} stock positions", **where)
        if bound:
            raise _refusal(409, "option_in_use", f"{bound} parts are bound to this option", **where)
    for group in dropped_groups:
        lines, held, bound = await _group_counts(db, group)
        if lines:
            raise _refusal(409, "group_in_use", f"Group chosen in {lines} order lines", group=group.id)
        if held:
            raise _refusal(409, "group_in_use", f"Group held by {held} stock positions", group=group.id)
        if bound:
            raise _refusal(409, "group_in_use", f"{bound} parts are bound to this group's options", group=group.id)

    unchanged = (
        not dropped_groups
        and not dropped_options
        and all(g.id is not None and all(o.id is not None for o in g.options) for g in draft)
        and [g.id for g in draft] == [g.id for g in current]
        and all(
            by_group[g.id].name == g.name
            and by_group[g.id].default_option_id == g.default
            and (g.requires_part is None or bool(by_group[g.id].requires_part) == g.requires_part)
            and [o.id for o in g.options] == [o.id for o in _ordered(by_group[g.id])]
            and all(options_by_id[o.id].name == o.name for o in g.options)
            for g in draft
        )
    )
    if unchanged:
        return False

    # (a) renames and marks, in place — ids and configuration keys stay (keys are built
    # from ids); the mark moves no line or position (it decides only new lines).
    for group in draft:
        if group.id is None:
            continue
        by_group[group.id].name = group.name
        if group.requires_part is not None:
            by_group[group.id].requires_part = group.requires_part
        for option in group.options:
            if option.id is not None:
                options_by_id[option.id].name = option.name
    await db.flush()
    # (b) new groups, the standard set BEFORE the lines record it.
    made_options: dict[str, int] = {}
    made_groups: dict[str, ProductVariantGroup] = {}
    for position, group in enumerate(draft):
        if group.id is not None:
            continue
        default_index = next(i for i, o in enumerate(group.options) if o.temp_id == group.default)
        created = await _insert_group(
            db,
            product_id,
            group.name,
            [o.name for o in group.options],
            default_index=default_index,
            position=position,
            requires_part=bool(group.requires_part),
        )
        made_groups[group.temp_id] = created
        for option, row in zip(group.options, _ordered(created), strict=True):
            made_options[option.temp_id] = row.id
        await line_config.add_group_to_lines(db, created)
    # (c) new options of existing groups.
    for group in draft:
        if group.id is None:
            continue
        target = by_group[group.id]
        for option in group.options:
            if option.id is None:
                row = ProductVariantOption(name=option.name, position=len(target.options))
                target.options.append(row)
                await db.flush()
                made_options[option.temp_id] = row.id
    # (d) standards of existing groups.
    for group in draft:
        if group.id is not None:
            if group.default is None:
                default = None
            elif isinstance(group.default, int):
                default = group.default
            else:
                default = made_options[group.default]
            by_group[group.id].default_option_id = default
    await db.flush()
    # (e) dropped options, (f) dropped groups — guarded above.
    for option in dropped_options:
        by_group[owner[option.id]].options.remove(option)
    await db.flush()
    for group in dropped_groups:
        group.default_option_id = None
        await db.flush()
        await db.delete(group)
    await db.flush()
    # (g) positions: the draft's order.
    for position, group in enumerate(draft):
        row = by_group[group.id] if group.id is not None else made_groups[group.temp_id]
        row.position = position
        ids = [o.id if o.id is not None else made_options[o.temp_id] for o in group.options]
        for index, option_id in enumerate(ids):
            next(o for o in row.options if o.id == option_id).position = index
    await db.flush()
    return True


async def _option_counts(db: AsyncSession, option_id: int) -> tuple[int, int, int]:
    """What refuses an option's delete: lines that chose it, positions that hold it,
    parts bound to it."""
    lines = await db.scalar(select(func.count()).where(ProjectLineChoice.option_id == option_id)) or 0
    held = await db.scalar(select(func.count()).where(StockItemChoice.option_id == option_id)) or 0
    return lines, held, await _bound_parts(db, [option_id])


async def _group_counts(db: AsyncSession, group: ProductVariantGroup) -> tuple[int, int, int]:
    """What refuses a group's delete: lines and positions with a choice in it, parts
    bound to its options."""
    lines = (
        await db.scalar(
            select(func.count(func.distinct(ProjectLineChoice.line_id))).where(ProjectLineChoice.group_id == group.id)
        )
        or 0
    )
    held = (
        await db.scalar(
            select(func.count(func.distinct(StockItemChoice.item_id))).where(StockItemChoice.group_id == group.id)
        )
        or 0
    )
    return lines, held, await _bound_parts(db, [o.id for o in group.options])
