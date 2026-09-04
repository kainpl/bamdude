"""Pure composition logic for products: what a plate yields, which part an
object name is, how parts merge (spec §Composition sync, §Data model).

Everything above :func:`recipes_for_products` is pure — no session, no I/O.
That helper (and the single-product wrapper beside it) is the exception, and
deliberately so: reading a product's plate files is the single step both
``routes/products.py::list_plates`` and ``services/plan_engine.py`` need, and
two copies of it would drift on the one question that matters (a trashed file's
plates are NOT printable). Plate yield is derived from
``LibraryFile.file_metadata`` every time — never cached.

⚠️ ``plates[].objects`` is a NAME-DEDUPLICATED list (ten cloned clips collapse
to one entry). Instances live in ``plates[].printable_objects`` (identify_id →
raw name), which is what every count here reads first.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.library import LibraryFile
from backend.app.models.product import Product, ProductPart, ProductPlate
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


def estimate_seconds(recipe: PlateRecipe) -> int | None:
    """The plate's print time, normalised to "an estimate or nothing".

    ⚠️ **A zero is not an instant plate — it is a file that carries no estimate**,
    and everything that reads a recipe's time must read it that way or the
    answers disagree with each other. They did, twice. Inside the plan engine
    :func:`_pick_key` scored a 0 as unknown (``secs or 1``) while its own
    tie-break read the same 0 as a real, unbeatable 0 s, and the row then
    reported ``time_unknown=False``, claiming an estimate it did not have. And
    ``routes/products.py::list_plates`` emitted the raw number, so a plate the
    plan called timeless showed ``0s`` in the "+ plate" menu that adds it to
    that same plan.

    It lives HERE, beside :class:`PlateRecipe`, for the reason the module
    docstring gives: the route and the engine read the same recipes, and a
    second copy of this rule is the copy that goes stale.
    """
    secs = recipe.print_time_seconds
    return secs if secs is not None and secs > 0 else None


def _plate_number(meta: dict | None, plate_index: int, key: str) -> int | float | None:
    plates = _plates(meta, plate_index)
    if plate_index > 0:
        return plates[0].get(key) if plates else None
    if len(plates) == 1:
        return plates[0].get(key)
    # Whole multi-plate file: sum the plates that HAVE the figure, the same
    # convention the library card totals use (routes/library.py, is_multi_plate
    # branch). ⚠️ The top-level key is only ONE plate's snapshot, so it is the
    # fallback of last resort — reading it for a half-sliced file would report
    # plate 1's time as the whole file's.
    numeric = [v for v in (p.get(key) for p in plates) if isinstance(v, (int, float))]
    if numeric:
        return sum(numeric)
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
    # Mirrors ``LibraryFile.is_printable()`` — BOTH of its branches. ``file_type``
    # alone is NOT the answer: ``detect_file_type`` collapses ``.gcode.3mf`` to
    # "gcode" by FILENAME and leaves "3mf" on packages that may or may not hold
    # gcode, so the content flag ``file_metadata["has_sliced_gcode"]`` (m137) is
    # what settles it. A MISSING flag counts as printable for a "gcode" row
    # (rows written before the check exists have no answer, and the filename
    # rule is what they were created under) but NOT for a "3mf" one, which needs
    # the flag to say yes. A single plate of a multi-plate file is decided by
    # its own timing alone — a file-level flag says nothing about which plates
    # carry gcode.
    ftype = (file_type or "").lower()
    has_gcode = (meta or {}).get("has_sliced_gcode")
    recipe.sliced = recipe.print_time_seconds is not None or (
        plate.plate_index == 0
        and ((ftype == "gcode" and has_gcode is not False) or (ftype == "3mf" and has_gcode is True))
    )
    return recipe


async def recipes_for_products(
    db: AsyncSession, products: Iterable[Product]
) -> dict[int, list[tuple[ProductPlate, LibraryFile, PlateRecipe]]]:
    """Every product's plates and recipes, in ONE round of queries.

    ⚠️ **This is the batch, and the single-product helper is a wrapper over
    it** — not the other way round. Both real callers work on a whole order:
    ``plan_engine.plan_for_order`` planned every line of it and the plan-enqueue
    handler validated every item of a request, and each of them asked per
    PRODUCT, so an order of five lines cost five identical-shaped SELECTs
    against ``library_files`` on a page that is recomputed on every read.

    ⚠️ ``LibraryFile.active()``: a trashed file is restorable, so its links and
    its ``product_plates`` rows stay — but its plates must not be offered as
    something to print, neither in the route's list nor in the plan engine's
    candidates. A plate whose file is gone (or trashed) is simply absent from
    the result; there is no placeholder to render or reason about.

    Every product asked about gets a key, empty list included, so a caller can
    index without guarding. Each list is ordered by ``ProductPlate.id`` — a
    stable sequence both callers see, so a plan row and a plate list can be
    compared by id. The route sorts the result for display itself.

    ``product.plates`` and ``product.parts`` must already be loaded (a lazy load
    inside an async session is a ``MissingGreenlet``, not a SELECT); both
    ``routes/products.py::_get`` and the engine's loader ``selectinload`` them.
    """
    products = list(products)
    plates_by_product = {product.id: list(product.plates or []) for product in products}
    file_ids = {plate.library_file_id for plates in plates_by_product.values() for plate in plates}
    files: dict[int, LibraryFile] = {}
    if file_ids:
        files = {
            f.id: f for f in (await db.execute(LibraryFile.active().where(LibraryFile.id.in_(file_ids)))).scalars()
        }
    out: dict[int, list[tuple[ProductPlate, LibraryFile, PlateRecipe]]] = {}
    for product in products:
        parts = list(product.parts or [])
        rows: list[tuple[ProductPlate, LibraryFile, PlateRecipe]] = []
        for plate in sorted(plates_by_product[product.id], key=lambda p: p.id):
            file = files.get(plate.library_file_id)
            if file is None:
                continue
            rows.append((plate, file, recipe_for(plate, file.file_metadata, file.file_type, parts)))
        out[product.id] = rows
    return out


async def recipes_for_product(
    db: AsyncSession, product: Product
) -> list[tuple[ProductPlate, LibraryFile, PlateRecipe]]:
    """One product's plates and recipes — :func:`recipes_for_products` for one.

    Kept for the single-product callers (``routes/products.py::list_plates`` and
    the plate routes beside it), which really do answer about one product. A
    caller holding SEVERAL must use the batch: calling this in a loop is the
    N+1 it exists to have removed.
    """
    return (await recipes_for_products(db, [product]))[product.id]


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
