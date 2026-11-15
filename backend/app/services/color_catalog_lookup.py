"""A Bambu roll's colour from the colour catalogue, and how a filament name names its product line.

One place for the question both inventories ask when the AMS identifies a
roll. The built-in auto-create (``spool_tag_matcher.create_spool_from_tray``)
carried the lookup inline — the sub-brand filter, the alpha guard — and the
Spoolman auto-create had none of it, so it matched a roll by material + hex
alone (upstream cb924e5c4, issue #2907).

No colour or product names live here: the colour name is the ``color_catalog``
row's, the product line is what the tray reports (or our family catalogue's
alias for its preset).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.color_catalog import ColorCatalogEntry
from backend.app.utils import filament_catalog

BAMBU_MANUFACTURER = "BAMBU LAB"


@dataclass(frozen=True)
class CatalogColor:
    """What the catalogue row says about a roll's colour.

    A row can carry gradient stops and a surface effect besides its name, which
    is how a roll the AMS identified is drawn like one picked by hand.
    """

    name: str
    extra_colors: str | None = None
    effect_type: str | None = None


async def resolve_bambu_color(db: AsyncSession, rgba: str | None, sub_brand: str | None) -> CatalogColor | None:
    """The catalogue's colour for this roll, or ``None`` when it has no row for it.

    Hex is not unique: ``#FFFFFF`` is "Jade White" in PLA Basic, "Ivory White"
    in PLA Matte and "White" in PLA Silk. The printer reports the product line
    as ``tray_sub_brands`` and the catalogue keeps the same string in its
    ``material`` column, so the two compare directly (upstream #1227). With no
    sub-brand the lowest id answers, deterministically on both databases.

    Alpha 00 is "Clear" before the catalogue is asked (#1545): it stores RGB
    only, so a clear roll's ``00000000`` would come back "Black".

    ``None`` is an answer, not a failure — the catalogue lags new colours, and a
    caller must cope rather than pick something.
    """
    if not rgba:
        return None
    if len(rgba) == 8 and rgba[6:8].lower() == "00":
        return CatalogColor("Clear")
    if len(rgba) < 6:
        return None
    query = (
        select(ColorCatalogEntry)
        .where(func.upper(ColorCatalogEntry.hex_color) == f"#{rgba[:6].upper()}")
        .where(func.upper(ColorCatalogEntry.manufacturer) == BAMBU_MANUFACTURER)
    )
    if sub_brand:
        query = query.where(func.upper(ColorCatalogEntry.material) == sub_brand.upper())
    entry = (await db.execute(query.order_by(ColorCatalogEntry.id).limit(1))).scalar_one_or_none()
    if entry is None:
        return None
    return CatalogColor(entry.color_name, entry.extra_colors, entry.effect_type)


def tray_product_line(tray_sub_brands: str | None, tray_info_idx: str | None) -> str:
    """The roll's product line ("PLA Matte"): the tray's own word, else our family catalogue's.

    A tray that reports no ``tray_sub_brands`` still names its preset; the
    family's alias is the line with the vendor in front ("Bambu PLA Matte").
    """
    reported = (tray_sub_brands or "").strip()
    if reported:
        return reported
    family = filament_catalog.get_family((tray_info_idx or "").strip()) if tray_info_idx else None
    if family is None or not family.alias:
        return ""
    alias = family.alias.strip()
    for prefix in filter(None, ((family.vendor or "").strip(), (family.vendor or "").split(" ")[0])):
        if alias.lower().startswith(prefix.lower() + " "):
            return alias[len(prefix) + 1 :].strip()
    return alias


def _normalise(name: str | None) -> str:
    # "Tough+ Black" / "Silk+ Gold" are the library's spelling of lines the
    # printer reports as "PLA Tough" / "PLA Silk": the plus belongs to the
    # product name, not the line.
    return " ".join((name or "").replace("+", " ").lower().split())


def _line_word(sub_brand: str | None, material: str | None) -> str:
    """The product line with the base material taken off: "PLA Matte" → "matte"."""
    line, base = _normalise(sub_brand), _normalise(material)
    return line[len(base) + 1 :] if base and line.startswith(base + " ") else ""


def product_line_rank(
    filament_name: str | None, color_name: str | None, sub_brand: str | None, material: str | None
) -> int | None:
    """How specifically a Spoolman filament's NAME identifies this roll's product line; ``None`` = it does not.

    The name is the only field of a Spoolman filament that carries the line.
    Three spellings count:

    0. the sub-brand itself ("PLA Matte") — how the filaments we create are named;
    1. the line folded into the colour ("Matte Charcoal", "Tough+ Black") — how
       SpoolmanDB names every Bambu entry outside PLA Basic;
    2. the bare colour ("Black") — how SpoolmanDB names PLA Basic and the lines
       with a base material of their own.

    Rank 2 cannot tell two lines of one material apart ("Black" is PLA Basic and
    PLA Tough alike); it is accepted because existing instances are full of
    filaments named that, and refusing it would duplicate every one of them
    (upstream's tolerance, taken as is).
    """
    name = _normalise(filament_name)
    if not name:
        return None
    if sub_brand and name == _normalise(sub_brand):
        return 0
    color = _normalise(color_name)
    if not color:
        return None
    line = _line_word(sub_brand, material)
    if line and name in (f"{line} {color}", f"{color} {line}"):
        return 1
    return 2 if name == color else None


def best_product_line_match(
    filaments: list[dict], color_name: str | None, sub_brand: str | None, material: str | None
) -> dict | None:
    """The filament whose name identifies the roll's line most specifically; the first wins a tie."""
    best: dict | None = None
    best_rank: int | None = None
    for filament in filaments:
        rank = product_line_rank(filament.get("name"), color_name, sub_brand, material)
        if rank is not None and (best_rank is None or rank < best_rank):
            best, best_rank = filament, rank
    return best
