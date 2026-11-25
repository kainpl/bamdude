"""A configuration on the wire — names, not sentences (spec workshop-product-variants, rule 20;
workshop-finished-goods, rule 16).

An order line and a stock position carry the same configuration shape, and
the frontend composes the caption from it in the reader's language. The
figures are ``line_composition``'s; this module only names them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.models.product import ProductPart
from backend.app.models.product_variant import ProductVariantGroup
from backend.app.schemas.project import LineChangedPartOut, LineChoiceOut, LineConfigurationOut
from backend.app.services.line_composition import LineConfig, composition, standard_per


async def groups_by_product(db: AsyncSession, product_ids: Iterable[int]) -> dict[int, list[ProductVariantGroup]]:
    """Every group of these products with its options, in their order — one statement."""
    ids = sorted(set(product_ids))
    out: dict[int, list[ProductVariantGroup]] = {pid: [] for pid in ids}
    if not ids:
        return out
    for group in (
        await db.execute(
            select(ProductVariantGroup)
            .options(selectinload(ProductVariantGroup.options))
            .where(ProductVariantGroup.product_id.in_(ids))
            .order_by(ProductVariantGroup.position, ProductVariantGroup.id)
        )
    ).scalars():
        out.setdefault(group.product_id, []).append(group)
    return out


def configuration_out(
    groups: Sequence[ProductVariantGroup],
    parts: Sequence[ProductPart],
    cfg: LineConfig,
    defaults: Mapping[int, int],
    mode: str = "product",
) -> LineConfigurationOut:
    """One configuration with names: the option of every group (the standard
    marked) and each changed count beside what the options give without it —
    for a parts line, the product's own count per unit."""
    ordered = sorted(parts, key=lambda p: (p.sort_order or 0, p.id))
    if mode == "parts":
        return LineConfigurationOut(
            changed_parts=[
                LineChangedPartOut(part_id=p.id, name=p.name, qty=cfg.counts[p.id], standard_qty=standard_per(p))
                for p in ordered
                if p.id in cfg.counts
            ]
        )
    chosen = {**defaults, **cfg.choices}
    choices: list[LineChoiceOut] = []
    for group in groups:
        option = next((o for o in group.options if o.id == chosen.get(group.id)), None)
        if option is not None:
            choices.append(
                LineChoiceOut(
                    group_id=group.id,
                    group_name=group.name,
                    option_id=option.id,
                    option_name=option.name,
                    is_default=option.id == group.default_option_id,
                )
            )
    base = {p.id: per for p, per in composition(ordered, "product", set(chosen.values()), {})}
    return LineConfigurationOut(
        choices=choices,
        changed_parts=[
            LineChangedPartOut(part_id=p.id, name=p.name, qty=cfg.counts[p.id], standard_qty=base.get(p.id, 0))
            for p in ordered
            if p.id in cfg.counts
        ],
    )


def saved_configuration(line) -> LineConfigurationOut | None:
    if not line.composition_revisions:
        return None
    latest = line.composition_revisions[-1]
    return LineConfigurationOut.model_validate(
        {**latest["configuration"], "revision": len(line.composition_revisions), "origin": latest["origin"]}
    )
