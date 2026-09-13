"""Filament needs — what the plan still asks for against what is on the shelf.

Spec: docs/superpowers/specs/2026-09-07-filament-needs-design.md. An ADVISORY
layer above the plan engine and the inventory: it reads the plan's rows and
the plate's slicer filaments, adds the order's pending queue rows, and puts
the sum per (material, line colour) next to the spools of either backend. It
reserves nothing and gates nothing; the plan engine stays ignorant of spools.

Unknown grams are counted, never defaulted (Decision 5): a plate without
filaments, or a filament without a type, is an unknown PRINT; a typed filament
without grams is an unknown print OF ITS KEY. Zero grams is an answer.

⚠️ **The queue side is deliberately a CONSERVATIVE over-count.** The loader adds
every pending row of the order, of both queue tiers; the plan engine subtracts
only the rows it can ATTRIBUTE — a row queued from an archive rather than a
library file, or a file that is no plate of the line's product, counts nothing
for it (see ``plan_engine.queued_yield_by_line``'s docstring). So a re-queued
archive print filed under an order is added here without ever having been
subtracted there, and the order's need over-states itself by that print's grams
until the print lands. Accepted on purpose: mirroring the engine's attribution
would mean a second copy of ``queued_yield_by_line`` living in an advisory
layer, and over-stating a need is the safe direction for a figure whose whole
job is «will the shelf hold out».
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from time import monotonic

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.routes.settings import get_setting
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.color_catalog import ColorCatalogEntry
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.product import ProductPlate
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.app.models.queue_source import QueueSource
from backend.app.models.spool import Spool
from backend.app.services.plan_engine import OrderPlan, plan_for_orders
from backend.app.services.product_composition import plate_filaments
from backend.app.services.queue_source_descriptor import stored_descriptor
from backend.app.services.queue_times import filaments_for_row
from backend.app.services.spoolman import get_spoolman_client

ASSUMPTIONS: tuple[str, ...] = ("slicer_estimate",)


@dataclass(frozen=True)
class NeedKey:
    material: str
    colour: str | None


def key_of(material: str | None, colour: str | None) -> NeedKey | None:
    """``(TYPE, colour)`` — type upper-cased, colour casefolded and trimmed, ``None`` when blank."""
    mat = (material or "").strip().upper()
    if not mat:
        return None
    col = (colour or "").strip().casefold()
    return NeedKey(mat, col or None)


@dataclass
class FilamentLine:
    material: str | None
    grams: float | None


@dataclass
class QueuedNeed:
    line_colour: str | None
    filaments: list[FilamentLine] | None  # None = the row has no readable plate


@dataclass
class SpoolStock:
    material: str
    colour_name: str | None
    colour_hex: str | None  # RRGGBB, lower-case, no '#'
    remaining_g: float


@dataclass
class Needs:
    grams: dict[NeedKey, float] = field(default_factory=dict)
    unknown_by_key: Counter[NeedKey] = field(default_factory=Counter)
    unknown_prints: int = 0

    def add(self, filaments: list[FilamentLine] | None, colour: str | None, prints: int) -> None:
        if not filaments:
            self.unknown_prints += prints
            return
        untyped = False
        for f in filaments:
            key = key_of(f.material, colour)
            if key is None:
                untyped = True
                continue
            if f.grams is None:
                self.unknown_by_key[key] += prints
            else:
                self.grams[key] = self.grams.get(key, 0.0) + prints * float(f.grams)
        if untyped:
            self.unknown_prints += prints

    def merge(self, other: Needs) -> Needs:
        out = Needs(dict(self.grams), Counter(self.unknown_by_key), self.unknown_prints)
        for key, g in other.grams.items():
            out.grams[key] = out.grams.get(key, 0.0) + g
        out.unknown_by_key.update(other.unknown_by_key)
        out.unknown_prints += other.unknown_prints
        return out

    def keys(self) -> set[NeedKey]:
        """Every key a row will be made for — the grams AND the gramless-but-typed ones."""
        return set(self.grams) | set(self.unknown_by_key)


def need_of_plan(
    plan: OrderPlan | None, line_colours: dict[int, str | None], plate_filaments: dict[int, list[FilamentLine]]
) -> Needs:
    """Σ count × grams per key over the plan's rows; ``plate_filaments`` is keyed by ``ProductPlate.id``."""
    needs = Needs()
    for line in plan.lines if plan else []:
        colour = line_colours.get(line.line_id)
        for row in line.rows:
            if row.count <= 0:
                continue
            needs.add(plate_filaments.get(row.plate_id), colour, row.count)
    return needs


def need_of_queue(rows: Iterable[QueuedNeed]) -> Needs:
    """Σ count × grams per key over the queue rows; each row counts as 1 print."""
    needs = Needs()
    for row in rows:
        needs.add(row.filaments, row.line_colour, 1)
    return needs


def _matches_colour(spool: SpoolStock, colour: str, names_of_hex: Callable[[str], set[str]]) -> bool:
    if spool.colour_name and spool.colour_name.strip().casefold() == colour:
        return True
    return bool(spool.colour_hex) and colour in names_of_hex(spool.colour_hex)


def stock_by_key(
    spools: list[SpoolStock], keys: Iterable[NeedKey], names_of_hex: Callable[[str], set[str]]
) -> dict[NeedKey, tuple[float, float]]:
    """``key → (have_g, have_type_g)``: the type total always, the colour figure when the key has one. Pass ``needs.keys()`` to ensure coverage."""
    out: dict[NeedKey, tuple[float, float]] = {}
    for key in keys:
        of_type = [s for s in spools if s.material.strip().upper() == key.material]
        type_total = sum(s.remaining_g for s in of_type)
        have = (
            type_total
            if key.colour is None
            else sum(s.remaining_g for s in of_type if _matches_colour(s, key.colour, names_of_hex))
        )
        out[key] = (have, type_total)
    return out


@dataclass
class NeedRow:
    material: str
    colour: str | None
    need_g: float
    have_g: float | None
    have_type_g: float | None
    short_g: float | None
    unknown_prints: int


def rows_of(needs: Needs, stock: dict[NeedKey, tuple[float, float]] | None) -> list[NeedRow]:
    """One row per key (grams and gramless-but-typed).

    ``stock`` comes from ``stock_by_key(spools, needs.keys(), …)``;
    a key it does not carry is reported as an unknown shelf (``None``),
    never as zero — a caller that forgets a key sees dashes, not a silent 0.
    """
    rows: list[NeedRow] = []
    for key in sorted(needs.keys(), key=lambda k: (k.material, k.colour or "")):
        need = round(needs.grams.get(key, 0.0), 1)
        have = have_type = short = None
        if stock is not None and key in stock:
            have, have_type = stock[key]
            have, have_type = round(have, 1), round(have_type, 1)
            short = round(max(0.0, need - have), 1)
        rows.append(NeedRow(key.material, key.colour, need, have, have_type, short, needs.unknown_by_key.get(key, 0)))
    return rows


@dataclass
class OrderNeeds:
    project_id: int
    rows: list[NeedRow]
    unknown_prints: int
    stock_unavailable: bool


@dataclass
class FarmRow(NeedRow):
    orders_count: int = 0


@dataclass
class FarmNeeds:
    rows: list[FarmRow]
    orders_count: int
    unknown_prints: int
    stock_unavailable: bool


def farm_of(per_order: dict[int, list[NeedRow]], *, unknown_prints: int, stock_unavailable: bool) -> FarmNeeds:
    """One row per key across every order: needs summed, the shelf figures taken once (same shelf for all)."""
    acc: dict[NeedKey, FarmRow] = {}
    for rows in per_order.values():
        for r in rows:
            key = NeedKey(r.material, r.colour)
            row = acc.get(key)
            if row is None:
                acc[key] = FarmRow(r.material, r.colour, r.need_g, r.have_g, r.have_type_g, None, r.unknown_prints, 1)
            else:
                row.need_g = round(row.need_g + r.need_g, 1)
                row.unknown_prints += r.unknown_prints
                row.orders_count += 1
    for row in acc.values():
        row.short_g = None if row.have_g is None else round(max(0.0, row.need_g - row.have_g), 1)
    ordered = sorted(acc.values(), key=lambda k: (k.material, k.colour or ""))
    return FarmNeeds(ordered, len(per_order), unknown_prints, stock_unavailable)


# ---------- the loader ----------

logger = logging.getLogger(__name__)


def _grams(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _lines_of(raw: list[dict] | None) -> list[FilamentLine] | None:
    if not raw:
        return None
    return [
        FilamentLine(material=(f.get("type") or None), grams=_grams(f.get("used_g")))
        for f in raw
        if isinstance(f, dict)
    ]


def _spoolman_colour_name(name: str | None, material: str) -> str | None:
    """Spoolman's ``Filament`` has no colour field of its own — the name is the only
    carrier, conventionally ``"<material> <colour>"`` (e.g. ``"PETG Black"``). Strip the
    material prefix so the remainder matches a line colour the way a local spool's plain
    ``color_name`` does. Mirrors ``services/spoolman.py::_filament_subtype_part`` /
    ``api/routes/_spoolman_helpers.py::_map_spoolman_spool``'s read-side derivation."""
    s = (name or "").strip()
    if material and s.upper().startswith(material.upper() + " "):
        s = s[len(material) + 1 :].strip()
    return s or None


async def line_colours_of(db: AsyncSession, project_ids: list[int]) -> dict[int, tuple[int, str | None]]:
    """``line_id → (project_id, colour)`` for every line of the orders, in one statement.

    The ORDER rides along because a queue row may carry a line without an order
    id and must still be attributed — see :func:`queued_needs_of`. Reading it
    here costs nothing: the statement already walks exactly these rows.
    """
    if not project_ids:
        return {}
    rows = (
        await db.execute(
            select(ProjectLine.id, ProjectLine.project_id, ProjectLine.color).where(
                ProjectLine.project_id.in_(project_ids)
            )
        )
    ).all()
    return {line_id: (project_id, colour) for line_id, project_id, colour in rows}


async def plate_filaments_of(db: AsyncSession, plate_ids: set[int]) -> dict[int, list[FilamentLine]]:
    if not plate_ids:
        return {}
    rows = (
        await db.execute(
            select(ProductPlate.id, ProductPlate.plate_index, LibraryFile.file_metadata)
            .join(LibraryFile, LibraryFile.id == ProductPlate.library_file_id)
            .where(ProductPlate.id.in_(list(plate_ids)))
        )
    ).all()
    return {pid: (_lines_of(plate_filaments(meta, index)) or []) for pid, index, meta in rows}


async def queued_needs_of(
    db: AsyncSession, project_ids: list[int], lines: dict[int, tuple[int, str | None]]
) -> dict[int, list[QueuedNeed]]:
    """The order's pending rows of BOTH queue tiers — exactly what the plan engine subtracts.

    ⚠️ **The predicate mirrors** :func:`plan_engine.queued_yield_by_line`, whose
    docstring owns it: "still waiting" is ``print_queue.status == 'pending'``
    AND ``auto_queue_items.status == 'pending' AND assigned_to_item_id IS
    NULL`` — an auto row already handed to a printer item is counted once,
    through that item. The auto tier is not optional here: «whole plan to
    queue» with no printer picked writes exactly those rows
    (``routes/projects.py`` → ``auto_queue_add.add_items_to_auto_queue``), and
    a loader that read only ``print_queue`` watched the need drop to zero the
    moment the button was pressed, with full spools on the shelf and no flag
    saying why (final review C1).

    ⚠️ **Selected by order id OR line id**, because a queue row may carry a
    LINE without an order id — the engine's line branch filters on the line
    alone for that very reason, so filtering on the order alone here subtracted
    such a row without adding it back (final review I4). A row that names a
    line is attributed to that line's order; one that names none, to its own.

    ⚠️ **Both reads select COLUMNS, never the entity.** An ``AutoQueueItem``
    drags its ``target_location`` in on ``lazy="selectin"`` — a
    ``printer_locations`` SELECT inside an advisory read is a lie about what
    this code does, and the plan engine refuses it for the same reason. The
    files and archives the rows point at are then read once each by id, so the
    statement count is fixed however long the queue is.

    See the module docstring for what this does NOT mirror: the engine's
    attribution, and therefore its conservative over-count.
    """
    if not project_ids:
        return {}
    line_ids = list(lines)
    active = set(project_ids)
    rows = list(
        (
            await db.execute(
                select(
                    PrintQueueItem.library_file_id,
                    PrintQueueItem.archive_id,
                    PrintQueueItem.plate_id,
                    PrintQueueItem.project_id,
                    PrintQueueItem.project_line_id,
                    PrintQueueItem.queue_source_id,
                    PrintQueueItem.source_snapshot,
                ).where(
                    PrintQueueItem.status == "pending",
                    or_(
                        PrintQueueItem.project_id.in_(project_ids),
                        PrintQueueItem.project_line_id.in_(line_ids),
                    ),
                )
            )
        ).all()
    ) + list(
        (
            await db.execute(
                select(
                    AutoQueueItem.library_file_id,
                    AutoQueueItem.archive_id,
                    AutoQueueItem.plate_id,
                    AutoQueueItem.project_id,
                    AutoQueueItem.project_line_id,
                    AutoQueueItem.queue_source_id,
                    AutoQueueItem.source_snapshot,
                ).where(
                    AutoQueueItem.status == "pending",
                    AutoQueueItem.assigned_to_item_id.is_(None),
                    or_(
                        AutoQueueItem.project_id.in_(project_ids),
                        AutoQueueItem.project_line_id.in_(line_ids),
                    ),
                )
            )
        ).all()
    )
    file_ids = {r[0] for r in rows if r[0] is not None}
    archive_ids = {r[1] for r in rows if r[1] is not None}
    files: dict[int, LibraryFile] = (
        {f.id: f for f in (await db.execute(select(LibraryFile).where(LibraryFile.id.in_(file_ids)))).scalars()}
        if file_ids
        else {}
    )
    archives: dict[int, PrintArchive] = (
        {a.id: a for a in (await db.execute(select(PrintArchive).where(PrintArchive.id.in_(archive_ids)))).scalars()}
        if archive_ids
        else {}
    )
    # m173: a row with a captured source is read from THAT — the file or archive it
    # came from may be gone, and until this read such a row reported "unknown" and
    # the order page could not say what it still needs. Loaded by id in one
    # statement, like the two above, so the statement count stays fixed however
    # long the queue is.
    blob_ids = {r[5] for r in rows if r[5] is not None}
    blobs: dict[int, QueueSource] = (
        {b.id: b for b in (await db.execute(select(QueueSource).where(QueueSource.id.in_(blob_ids)))).scalars()}
        if blob_ids
        else {}
    )
    out: dict[int, list[QueuedNeed]] = {}
    for library_file_id, archive_id, plate_id, project_id, line_id, blob_id, snapshot in rows:
        owner, colour = lines.get(line_id, (project_id, None)) if line_id is not None else (project_id, None)
        if owner not in active:
            continue
        blob = blobs.get(blob_id) if blob_id is not None else None
        raw = filaments_for_row(
            archive=archives.get(archive_id) if archive_id is not None else None,
            library_file=files.get(library_file_id) if library_file_id is not None else None,
            plate_id=plate_id,
            descriptor=stored_descriptor(blob, snapshot) if blob is not None else None,
        )
        out.setdefault(owner, []).append(QueuedNeed(colour, _lines_of(raw)))
    return out


SPOOLMAN_SHELF_TTL_S = 30.0
"""How long a Spoolman answer — or its failure — stands for every reader.

The UI's own ``staleTime`` on both filament queries, so a shelf figure is never
older on screen than it would have been anyway. The point is the FAILURE:
Spoolman down costs three 5 s connect attempts, and paying ~16 s of a held
session per request made an order page unusable because a service it does not
own is unreachable (final review I6). Now that price is paid once per half
minute. The INTERNAL shelf is never memoised — it is one statement, and a spool
edited on the inventory page must show on the order page at once.
"""

_SPOOLMAN_SHELF: tuple[float, list[SpoolStock], bool] | None = None


def reset_spoolman_shelf_cache() -> None:
    """Forget the memoised Spoolman shelf. For tests, and for anything that must not wait out the TTL."""
    global _SPOOLMAN_SHELF
    _SPOOLMAN_SHELF = None


async def _spoolman_stock() -> tuple[list[SpoolStock], bool]:
    """One Spoolman round trip mapped to ``SpoolStock``; a dead one is reported, never raised."""
    client = await get_spoolman_client()
    if client is None:
        # Not an error the user made: the setting is on and the client is not
        # built (no URL, or the integration failed to start). Logged so the
        # «shelf could not be read» banner has something behind it.
        logger.info("filament needs: Spoolman is enabled but no client is configured")
        return [], True
    try:
        raw = await client.get_all_spools()
    except Exception as exc:  # noqa: BLE001 — the shelf is optional; the need is still shown
        logger.warning("filament needs: Spoolman did not answer: %s", exc)
        return [], True
    spools: list[SpoolStock] = []
    for s in raw or []:
        if not isinstance(s, dict) or s.get("archived"):
            continue
        fil = s.get("filament") or {}
        material = (fil.get("material") or "").strip().upper()
        if not material:
            continue
        hex_colour = (fil.get("color_hex") or "").replace("#", "").lower()[:6] or None
        colour_name = _spoolman_colour_name(fil.get("name"), material)
        spools.append(SpoolStock(material, colour_name, hex_colour, max(0.0, float(s.get("remaining_weight") or 0.0))))
    return spools, False


async def load_stock(db: AsyncSession) -> tuple[list[SpoolStock], bool]:
    """Live spools of the active backend; ``(spools, unavailable)`` — a dead Spoolman is reported, never raised."""
    if ((await get_setting(db, "spoolman_enabled")) or "").lower() == "true":
        global _SPOOLMAN_SHELF
        shelf = _SPOOLMAN_SHELF
        if shelf is not None and monotonic() - shelf[0] < SPOOLMAN_SHELF_TTL_S:
            return list(shelf[1]), shelf[2]
        spools, unavailable = await _spoolman_stock()
        _SPOOLMAN_SHELF = (monotonic(), spools, unavailable)
        return list(spools), unavailable
    rows = (await db.execute(select(Spool).where(Spool.archived_at.is_(None)))).scalars().all()
    return [
        SpoolStock(
            (spool.material or "").strip().upper(),
            spool.color_name,
            ((spool.rgba or "").replace("#", "").lower()[:6] or None),
            max(0.0, float(spool.label_weight or 0) - float(spool.weight_used or 0.0)),
        )
        for spool in rows
        if spool.material
    ], False


async def names_of_hex_loader(db: AsyncSession) -> Callable[[str], set[str]]:
    rows = (await db.execute(select(ColorCatalogEntry.hex_color, ColorCatalogEntry.color_name))).all()
    table: dict[str, set[str]] = {}
    for hex_colour, name in rows:
        key = (hex_colour or "").replace("#", "").lower()[:6]
        if key and name:
            table.setdefault(key, set()).add(name.strip().casefold())
    return lambda h: table.get((h or "").replace("#", "").lower()[:6], set())


async def needs_of_orders(db: AsyncSession, project_ids: list[int]) -> dict[int, OrderNeeds]:
    """Per order: the plan's need + the pending queue's need, against the shelf. An unknown id is absent."""
    if not project_ids:
        return {}
    status_of = dict((await db.execute(select(Project.id, Project.status).where(Project.id.in_(project_ids)))).all())
    active = [pid for pid in project_ids if status_of.get(pid) == "active"]
    if not active:
        # No target, no shelf. Every answer here is an empty list whatever the
        # spools say, and reading them would spend a Spoolman round trip — up
        # to ~16 s when it is down — to decorate nothing (final review I6).
        return {pid: OrderNeeds(pid, [], 0, False) for pid in project_ids if pid in status_of}
    plans = await plan_for_orders(db, active)
    plate_ids = {row.plate_id for plan in plans.values() for line in plan.lines for row in line.rows}
    lines = await line_colours_of(db, active)
    line_colours = {line_id: colour for line_id, (_, colour) in lines.items()}
    filaments = await plate_filaments_of(db, plate_ids)
    queued = await queued_needs_of(db, active, lines)
    spools, unavailable = await load_stock(db)
    names_of_hex = await names_of_hex_loader(db)
    out: dict[int, OrderNeeds] = {}
    for pid in project_ids:
        if pid not in status_of:
            continue
        if pid not in active:
            out[pid] = OrderNeeds(pid, [], 0, unavailable)
            continue
        needs = need_of_plan(plans.get(pid), line_colours, filaments).merge(need_of_queue(queued.get(pid, [])))
        stock = None if unavailable else stock_by_key(spools, needs.keys(), names_of_hex)
        out[pid] = OrderNeeds(pid, rows_of(needs, stock), needs.unknown_prints, unavailable)
    return out


async def needs_of_farm(db: AsyncSession) -> FarmNeeds:
    active = [pid for (pid,) in (await db.execute(select(Project.id).where(Project.status == "active"))).all()]
    per_order = await needs_of_orders(db, active)
    return farm_of(
        {pid: o.rows for pid, o in per_order.items()},
        unknown_prints=sum(o.unknown_prints for o in per_order.values()),
        stock_unavailable=any(o.stock_unavailable for o in per_order.values()),
    )
