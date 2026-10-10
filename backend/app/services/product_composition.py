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
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.library import LibraryFile
from backend.app.models.product import Product, ProductPart, ProductPlate
from backend.app.services.part_names import canonicalize, name_key
from backend.app.utils.printer_models import normalize_model_name

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


def plate_objects(meta: dict | None, plate_index: int) -> dict[int, str]:
    """``identify_id -> raw name`` of a plate's instances (spec part-thumbnails §10.1).

    The same plates and the same fallback as :func:`plate_instance_names`: a plate that lists
    names without ids (``objects``) has instances nobody can point at, so it adds names there
    and nothing here. JSON storage turned the ids into strings; an id that is not a u32 is
    skipped -- no slicer names such an object.
    """
    out: dict[int, str] = {}

    def take(objects: dict) -> None:
        for key, name in objects.items():
            try:
                identify_id = int(key)
            except (TypeError, ValueError):
                continue
            if 0 <= identify_id <= 0xFFFFFFFF:
                out[identify_id] = str(name)

    named = False
    for plate in _plates(meta, plate_index):
        po = plate.get("printable_objects")
        if isinstance(po, dict) and po:
            named = True
            take(po)
        elif plate.get("objects"):
            named = True
    if not named and plate_index == 0:
        po = (meta or {}).get("printable_objects")
        if isinstance(po, dict):
            take(po)
    return out


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
    #: The printer model the 3MF was sliced for, in the short spelling the
    #: auto-queue routes on (``AutoQueueItem.target_model`` →
    #: ``normalize_model_name``). It is a property of the FILE, not of one
    #: plate: ``file_metadata["sliced_for_model"]`` is written once per 3MF by
    #: ``ThreeMFParser``, so every plate of a file carries the same answer.
    #: ``None`` when the file names no model — which is not "any model", only
    #: "we do not know".
    printer_model: str | None = None


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
    # ⚠️ Normalised AGAIN, on purpose. ``ThreeMFParser`` already writes a short
    # name into ``sliced_for_model`` for a file it parsed itself, but rows
    # ingested by older code (and by the git restore) carry whatever the 3MF
    # said — a raw "Bambu Lab X1 Carbon", or an internal code like "C12". The
    # value has to compare equal to the auto-queue's ``target_model``, which is
    # run through this same function, or a plate the operator switched to would
    # be routed to no printer at all.
    raw_model = (meta or {}).get("sliced_for_model")
    recipe.printer_model = normalize_model_name(raw_model) if isinstance(raw_model, str) else None
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
    # ⚠️ The flag is a TRI-STATE and only a bool is inside its domain: yes, no,
    # or "this row has no answer". It comes out of JSON, so anything can be in
    # there — a ``0`` or a ``"false"`` written by some other writer is neither a
    # yes nor a no. This changes no answer: ``is not False`` / ``is True`` already
    # read every out-of-domain value the way ``None`` reads, because the identity
    # tests only ever recognise the two bool singletons. Normalising says so in
    # the code rather than in a reader's head, and "no answer" is the state a row
    # written before m137 is already in.
    raw_has_gcode = (meta or {}).get("has_sliced_gcode")
    has_gcode = raw_has_gcode if isinstance(raw_has_gcode, bool) else None
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
        plates = plates_by_product[product.id]
        if not plates:
            # Nothing to build a recipe from, so ``product.parts`` is never
            # read — which is the point: the docstring's "must already be
            # loaded" is a promise about a lazy load being a ``MissingGreenlet``
            # here, and a product with no plates must not be the one that trips
            # it for a question whose answer is empty either way.
            out[product.id] = []
            continue
        parts = list(product.parts or [])
        rows: list[tuple[ProductPlate, LibraryFile, PlateRecipe]] = []
        for plate in sorted(plates, key=lambda p: p.id):
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


@dataclass
class PartSource:
    """One plate a part can be printed from (WS-13 E1 PS1). A file the caller may not
    see in the library keeps every number — plate, model, yield, time, grams (Z7) —
    and loses its name and folder (``hidden``, LV4)."""

    plate_id: int
    library_file_id: int
    filename: str | None
    folder_id: int | None
    folder_name: str | None
    hidden: bool
    plate_index: int
    printer_model: str | None
    sliced: bool
    yield_: int
    print_time_seconds: int | None
    filament_used_grams: float | None
    recommended: bool = False


def part_sources(
    rows: Iterable[tuple[ProductPlate, LibraryFile, PlateRecipe]],
    visible: Callable[[LibraryFile], bool],
    folder_names: Mapping[int, str] | None = None,
) -> dict[int, list[PartSource]]:
    """``part_id → its sources`` over :func:`recipes_for_products` rows — pure.

    A plate is a source of every part it yields. Sliced plates come first, in the
    plan's order (``plan_engine.rank_key`` on the part's own yield), the first of them
    ``recommended``; unsliced plates after, by plate id — shown, never recommended.
    ``visible`` is the library's rule for this caller (``library.file_name_visible``);
    ``folder_names`` names the files' folders, read by the caller in one statement.
    """
    from backend.app.services.plan_engine import rank_key

    folders = folder_names or {}
    by_part: dict[int, list[PartSource]] = {}
    for plate, file, recipe in rows:
        shown = visible(file)
        for part_id, n in recipe.yield_by_part.items():
            if n <= 0:
                continue
            by_part.setdefault(part_id, []).append(
                PartSource(
                    plate_id=plate.id,
                    library_file_id=file.id,
                    filename=file.filename if shown else None,
                    folder_id=file.folder_id if shown else None,
                    folder_name=folders.get(file.folder_id) if shown and file.folder_id is not None else None,
                    hidden=not shown,
                    plate_index=plate.plate_index,
                    printer_model=recipe.printer_model,
                    sliced=recipe.sliced,
                    yield_=n,
                    print_time_seconds=estimate_seconds(recipe),
                    filament_used_grams=recipe.filament_used_grams,
                )
            )
    for sources in by_part.values():
        sources.sort(
            key=lambda s: (0, rank_key(s.yield_, s.print_time_seconds, s.plate_id)) if s.sliced else (1, (s.plate_id,))
        )
        if sources and sources[0].sliced:
            sources[0].recommended = True
    return by_part


def source_summary(sources: Iterable[PartSource]) -> dict:
    """What a part's row says about its sources (PS2): whether one is sliced, the
    sliced yields' range, how many are hidden, and the models they are sliced for
    (K3) — hidden ones included, a model is no file name (Z7)."""
    sources = list(sources)
    sliced = [s for s in sources if s.sliced]
    return {
        "has_sliced_source": bool(sliced),
        "yield_min": min((s.yield_ for s in sliced), default=None),
        "yield_max": max((s.yield_ for s in sliced), default=None),
        "hidden_sources": sum(1 for s in sources if s.hidden),
        "models": sorted({s.printer_model for s in sliced if s.printer_model}),
    }


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


class AliasTaken(ValueError):
    """The key already resolves to another part — its own key or one of its aliases.

    A ``ValueError`` still, so every older caller that catches one keeps working; the
    routes read ``key`` and ``owner`` to say it in a sentence the API-error catalog
    translates (WS-13 E10 A04 — ``str(e)`` never reached the catalog)."""

    def __init__(self, key: str, owner: str):
        super().__init__(f"'{key}' already belongs to part '{owner}'")
        self.key = key
        self.owner = owner


class OwnKeyAlias(ValueError):
    """A part's own key is not an alias it can drop."""

    def __init__(self) -> None:
        super().__init__("A part cannot drop its own key")


class AliasTooLong(ValueError):
    """An alias, normalised, is longer than ``name_key`` holds."""

    def __init__(self) -> None:
        super().__init__("An alias is too long")


#: ``name_key``'s column — a part's key and every alias fit in it (WS-13 E10 A03).
KEY_MAX = 512


def normalise_alias(raw: str) -> str:
    """An alias as the routes have always stored it: trimmed and lower-cased."""
    return (raw or "").strip().lower()


def add_alias(parts: Iterable[ProductPart], target: ProductPart, key: str) -> None:
    if len(key) > KEY_MAX:
        raise AliasTooLong()
    owner = part_index(parts).get(key)
    if owner is not None and owner is not target:
        raise AliasTaken(key, owner.name)
    aliases = list(target.aliases or [target.name_key])
    if key not in aliases:
        aliases.append(key)
    target.aliases = aliases
    target.auto = False


def remove_alias(target: ProductPart, key: str) -> None:
    """Drop one alias — and leave the list in the one spelling the writers use.

    ``add_alias`` and ``merge_parts`` both write ``aliases or [target.name_key]``,
    so a part that HAS a list always carries its own key in it. Removing the
    last other alias used to leave a bare ``[]``: the same fact in the spelling
    nothing else writes, which reads to anyone comparing two parts as "this one
    lost its key". A part that had no aliases keeps not having any — the column
    lands on ``[]``, the empty list, and NOT on its own key: aliases are
    printed-only (a purchased part has none), and granting one here is not this
    function's business.
    """
    if key == target.name_key:
        raise OwnKeyAlias()
    had_list = bool(target.aliases)
    remaining = [a for a in (target.aliases or []) if a != key]
    target.aliases = remaining or ([target.name_key] if had_list else [])
    target.auto = False


def set_aliases(parts: Iterable[ProductPart], target: ProductPart, desired: Iterable[str]) -> None:
    """Replace a printed part's aliases with ``desired`` — the part dialog's whole list
    in one save (WS-13 E10 A05).

    Each entry is normalised as the single routes do (:func:`normalise_alias`); blanks
    are dropped, duplicates collapse, and the part's own key stays first whatever the
    list says. The writes go through :func:`remove_alias` and :func:`add_alias`, so a
    key another part owns refuses (:class:`AliasTaken`) exactly as the single route
    does — the caller's transaction rolls back, nothing is half-written."""
    parts = list(parts)
    wanted: list[str] = []
    for raw in desired:
        key = normalise_alias(raw)
        if key and key != target.name_key and key not in wanted:
            wanted.append(key)
    for key in [a for a in (target.aliases or []) if a != target.name_key and a not in wanted]:
        remove_alias(target, key)
    for key in wanted:
        if key not in (target.aliases or []):
            add_alias(parts, target, key)
    if not target.aliases:
        target.aliases = [target.name_key]
