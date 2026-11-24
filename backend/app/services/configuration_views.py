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
from backend.app.services.line_composition import LineConfig, composition, composition_from_snapshot, standard_per


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


def changed_part_identity(row: dict, parts: Sequence[dict]) -> str | None:
    """Resolve descriptions written before they stored their part identity."""
    if "identity" in row:
        return row["identity"]
    matches = [part for part in parts if part["id"] == row["part_id"]]
    if len(matches) > 1:
        matches = [part for part in matches if part["name"] == row["name"]]
    return matches[-1]["identity"] if matches else None


def saved_configuration(line, catalog: Sequence[ProductPart], config: LineConfig) -> LineConfigurationOut | None:
    if not line.composition_revisions:
        return None
    latest = line.composition_revisions[-1]
    parts = latest["parts"]
    projected = composition_from_snapshot(line.product_id, parts, catalog)
    live = {part.id: part.composition_identity for part in catalog if not part.ignored}
    saved_parts = [
        {
            "part_id": part.id,
            "identity": part.composition_identity,
            "name": part.name,
            "kind": part.kind,
            "qty": qty,
            "in_catalog": live.get(part.id) == part.composition_identity,
            "manual": row.get("manual", False),
        }
        for (part, qty), row in zip(projected, parts, strict=True)
        if not part.ignored
    ]
    # Live rows form the editor's lookup. Archived rows keep their document order.
    saved_parts.sort(key=lambda row: (not row["in_catalog"], row["part_id"] if row["in_catalog"] else 0))
    view_ids = {
        **{identity: pid for pid, identity in live.items()},
        **{row["identity"]: row["part_id"] for row in saved_parts},
    }
    next_archived = min(0, *(row["part_id"] for row in saved_parts)) - 1 if saved_parts else -1
    changed_parts = []
    count_identities = {}
    for row in latest["configuration"]["changed_parts"]:
        identity = changed_part_identity(row, parts)
        part_id = view_ids.get(identity)
        if part_id is None:
            # Older revisions did not record the identity of a zero override.
            # A surviving count row still belongs to the live part: catalog deletion clears it.
            legacy_live = "identity" not in row and identity is None and config.counts.get(row["part_id"]) == row["qty"]
            if legacy_live and row["part_id"] in live:
                part_id = row["part_id"]
                identity = live[part_id]
            else:
                part_id = next_archived
                next_archived -= 1
        changed_parts.append({**row, "part_id": part_id})
        if identity is not None and live.get(part_id) == identity:
            count_identities[part_id] = identity
    return LineConfigurationOut.model_validate(
        {
            **latest["configuration"],
            "changed_parts": changed_parts,
            "saved_parts": saved_parts,
            "count_identities": count_identities,
            "revision": len(line.composition_revisions),
            "origin": latest["origin"],
        }
    )
