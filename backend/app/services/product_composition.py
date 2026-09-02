"""Pure composition logic for products: what a plate yields, which part an
object name is, how parts merge (spec §Composition sync, §Data model).

Nothing here touches the database. Plate yield is derived from
``LibraryFile.file_metadata`` every time — never cached.

⚠️ ``plates[].objects`` is a NAME-DEDUPLICATED list (ten cloned clips collapse
to one entry). Instances live in ``plates[].printable_objects`` (identify_id →
raw name), which is what every count here reads first.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from backend.app.models.product import ProductPart, ProductPlate
from backend.app.services.part_names import canonicalize, name_key

PURCHASED_KEY_PREFIX = "purchased:"


def purchased_name_key(name: str) -> str:
    return PURCHASED_KEY_PREFIX + " ".join((name or "").split()).lower()


def _plates(meta: dict | None, plate_index: int) -> list[dict]:
    plates = [p for p in ((meta or {}).get("plates") or []) if isinstance(p, dict)]
    if plate_index > 0:
        return [p for p in plates if p.get("index") == plate_index]
    return plates


def plate_instance_names(meta: dict | None, plate_index: int) -> list[str]:
    """Raw object names, one per instance. Plate 0 = the whole file."""
    names: list[str] = []
    for plate in _plates(meta, plate_index):
        po = plate.get("printable_objects")
        if isinstance(po, dict) and po:
            names.extend(str(v) for v in po.values())
        else:
            names.extend(str(v) for v in (plate.get("objects") or []))
    if not names and plate_index == 0:
        po = (meta or {}).get("printable_objects")
        if isinstance(po, dict):
            names.extend(str(v) for v in po.values())
    return names


def plate_key_counts(meta: dict | None, plate_index: int) -> tuple[Counter[str], dict[str, str]]:
    """``name_key → instances`` and ``name_key → canonical display spelling``."""
    raw = plate_instance_names(meta, plate_index)
    counts: Counter[str] = Counter()
    display: dict[str, str] = {}
    for r in raw:
        canon = canonicalize(r, raw)
        key = name_key(canon)
        counts[key] += 1
        display.setdefault(key, canon)
    return counts, display


def plate_filaments(meta: dict | None, plate_index: int) -> list[dict]:
    out: list[dict] = []
    for plate in _plates(meta, plate_index):
        out.extend(f for f in (plate.get("filaments") or []) if isinstance(f, dict))
    return out


def plate_materials(meta: dict | None, plate_index: int) -> set[str]:
    """Filament type tokens, upper-cased — the values ``ProjectLine.material`` matches against."""
    return {str(f.get("type")).strip().upper() for f in plate_filaments(meta, plate_index) if f.get("type")}


def plate_colors(meta: dict | None, plate_index: int) -> set[str]:
    return {str(f.get("color")).strip().upper() for f in plate_filaments(meta, plate_index) if f.get("color")}


def part_index(parts: Iterable[ProductPart]) -> dict[str, ProductPart]:
    """Every key that resolves to a part: its own ``name_key`` and every alias."""
    idx: dict[str, ProductPart] = {}
    for part in parts:
        idx[part.name_key] = part
        for alias in part.aliases or []:
            idx[alias] = part
    return idx


@dataclass
class PlateRecipe:
    library_file_id: int
    plate_index: int
    sliced: bool
    yield_by_part: dict[int, int] = field(default_factory=dict)  # part_id → instances
    unassigned: dict[str, int] = field(default_factory=dict)  # name_key → instances no part covers
    materials: set[str] = field(default_factory=set)
    colors: set[str] = field(default_factory=set)
    print_time_seconds: int | None = None
    filament_used_grams: float | None = None


def _plate_number(meta: dict | None, plate_index: int, key: str):
    plates = _plates(meta, plate_index)
    if plate_index > 0:
        return plates[0].get(key) if plates else None
    if len(plates) == 1:
        return plates[0].get(key)
    # whole multi-plate file: sum when every plate knows the number
    vals = [p.get(key) for p in plates]
    if plates and all(isinstance(v, (int, float)) for v in vals):
        return sum(vals)
    return (meta or {}).get(key)


def recipe_for(
    plate: ProductPlate, meta: dict | None, file_type: str | None, parts: Iterable[ProductPart]
) -> PlateRecipe:
    counts, _display = plate_key_counts(meta, plate.plate_index)
    idx = part_index(parts)
    recipe = PlateRecipe(library_file_id=plate.library_file_id, plate_index=plate.plate_index, sliced=False)
    for key, n in counts.items():
        part = idx.get(key)
        if part is None:
            recipe.unassigned[key] = n
        else:
            recipe.yield_by_part[part.id] = recipe.yield_by_part.get(part.id, 0) + n
    secs = _plate_number(meta, plate.plate_index, "print_time_seconds")
    grams = _plate_number(meta, plate.plate_index, "filament_used_grams")
    recipe.print_time_seconds = int(secs) if isinstance(secs, (int, float)) else None
    recipe.filament_used_grams = float(grams) if isinstance(grams, (int, float)) else None
    recipe.materials = plate_materials(meta, plate.plate_index)
    recipe.colors = plate_colors(meta, plate.plate_index)
    # A plate is printable when its own gcode exists (per-plate timing) or the
    # file as a whole is a sliced container (file_type 'gcode').
    recipe.sliced = recipe.print_time_seconds is not None or (file_type or "").lower() == "gcode"
    return recipe


def merge_parts(target: ProductPart, source: ProductPart) -> None:
    """Absorb ``source`` into ``target``: aliases union, target keeps its qty and
    name. The caller deletes ``source`` and re-syncs nothing — history rows now
    resolve to ``target`` through the union."""
    merged = list(target.aliases or [target.name_key])
    for key in [source.name_key, *(source.aliases or [])]:
        if key not in merged:
            merged.append(key)
    target.aliases = merged
    target.auto = False


def add_alias(parts: Iterable[ProductPart], target: ProductPart, key: str) -> None:
    owner = part_index(parts).get(key)
    if owner is not None and owner is not target:
        raise ValueError(f"'{key}' already belongs to part '{owner.name}'")
    aliases = list(target.aliases or [target.name_key])
    if key not in aliases:
        aliases.append(key)
    target.aliases = aliases
    target.auto = False


def remove_alias(target: ProductPart, key: str) -> None:
    if key == target.name_key:
        raise ValueError("a part cannot drop its own key")
    target.aliases = [a for a in (target.aliases or []) if a != key]
    target.auto = False
