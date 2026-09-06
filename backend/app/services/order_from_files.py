"""Orders from files (spec 2026-09-06): the parts preview and the three ways a
product + order come out of library files without anybody authoring them.

Nothing here knows the operator's language: every refusal is an exception
class, and the ROUTE turns it into the English sentence the catalogue
translates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import reduce
from math import gcd
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.models.library import LibraryFile
from backend.app.models.product import Product, ProductOrigin, ProductPart, ProductPlate, product_files
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.app.services.product_composition import estimate_seconds, plate_key_counts, recipe_for
from backend.app.services.product_sync import is_plan_eligible, sync_product_for_file, wanted_plate_indices


class OrderFromFilesError(Exception):
    """Base of every refusal below; the route maps subclasses to sentences."""


class FileNotFound(OrderFromFilesError): ...


class NotPlannable(OrderFromFilesError): ...


class NoTargets(OrderFromFilesError): ...


class UnknownPartKey(OrderFromFilesError):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


class PlateNotFound(OrderFromFilesError): ...


class DuplicatePlate(OrderFromFilesError): ...


class NotACatalogProduct(OrderFromFilesError): ...


class FilesNotLinked(OrderFromFilesError): ...


# ---------- preview ----------


@dataclass
class PreviewPlate:
    plate_index: int
    sliced: bool
    print_time_seconds: int | None


@dataclass
class PreviewFile:
    id: int
    filename: str
    sliced_for_model: str | None
    plates: list[PreviewPlate] = field(default_factory=list)


@dataclass
class PreviewYield:
    library_file_id: int
    plate_index: int
    count: int


@dataclass
class PreviewPart:
    name_key: str
    name: str
    yields: list[PreviewYield] = field(default_factory=list)


@dataclass
class PartsPreview:
    files: list[PreviewFile]
    parts: list[PreviewPart]
    catalog_product: Product | None  # parts loaded


def file_stem(filename: str) -> str:
    """``lamp.gcode.3mf`` → ``lamp`` — the rule ``POST /products/from-file`` uses."""
    stem = Path(filename).name
    for suffix in (".gcode.3mf", ".3mf", ".gcode"):
        if stem.lower().endswith(suffix):
            return stem[: -len(suffix)]
    return stem


async def _active_files(db: AsyncSession, file_ids: list[int]) -> list[LibraryFile]:
    """The files in the ORDER asked for; a missing (or trashed) id is a refusal,
    not a silently shorter answer."""
    rows = {f.id: f for f in (await db.execute(LibraryFile.active().where(LibraryFile.id.in_(file_ids)))).scalars()}
    out: list[LibraryFile] = []
    for file_id in dict.fromkeys(file_ids):
        if file_id not in rows:
            raise FileNotFound(file_id)
        out.append(rows[file_id])
    return out


async def parts_preview(db: AsyncSession, file_ids: list[int]) -> PartsPreview:
    """Read-only: what the selected files make, unified by canonical part key
    (the SAME canonicalisation the sync seeds parts with, so a key sent back to
    ``create_job_order`` matches the part the sync will create), and the one
    catalogue product that links every one of them, if exactly one does."""
    files = await _active_files(db, file_ids)
    for f in files:
        if not is_plan_eligible(f.file_type):
            raise NotPlannable(f.id)
    out_files: list[PreviewFile] = []
    parts: dict[str, PreviewPart] = {}
    for f in files:
        meta = f.file_metadata
        model = meta.get("sliced_for_model") if isinstance(meta, dict) else None
        pf = PreviewFile(id=f.id, filename=f.filename, sliced_for_model=model if isinstance(model, str) else None)
        for plate_index in sorted(wanted_plate_indices(meta)):
            # A transient ProductPlate (never added to the session) is all
            # ``recipe_for`` reads: the file and the index.
            recipe = recipe_for(ProductPlate(library_file_id=f.id, plate_index=plate_index), meta, f.file_type, [])
            pf.plates.append(
                PreviewPlate(plate_index=plate_index, sliced=recipe.sliced, print_time_seconds=estimate_seconds(recipe))
            )
            counts, display = plate_key_counts(meta, plate_index)
            for key, n in counts.items():
                part = parts.setdefault(key, PreviewPart(name_key=key, name=display[key]))
                part.yields.append(PreviewYield(library_file_id=f.id, plate_index=plate_index, count=n))
        out_files.append(pf)
    ids = [f.id for f in files]
    full = [
        pid
        for pid, n in (
            await db.execute(
                select(product_files.c.product_id, func.count(product_files.c.library_file_id))
                .where(product_files.c.library_file_id.in_(ids))
                .group_by(product_files.c.product_id)
            )
        ).all()
        if n == len(ids)
    ]
    catalog = (
        (
            await db.execute(
                select(Product)
                .options(selectinload(Product.parts))
                .where(Product.id.in_(full), Product.origin == ProductOrigin.CATALOG.value)
            )
        )
        .scalars()
        .all()
        if full
        else []
    )
    return PartsPreview(
        files=out_files, parts=list(parts.values()), catalog_product=catalog[0] if len(catalog) == 1 else None
    )


# ---------- creating product + order ----------


async def _linked_product_ids(db: AsyncSession, library_file_id: int) -> set[int]:
    return set(
        (await db.execute(select(product_files.c.product_id).where(product_files.c.library_file_id == library_file_id)))
        .scalars()
        .all()
    )


async def _link(db: AsyncSession, library_file_id: int, product_id: int) -> None:
    """Add the file to the product through the one door, as a UNION with the
    products it already belongs to (inv-product-links-single-writer)."""
    desired = await _linked_product_ids(db, library_file_id) | {product_id}
    await sync_product_for_file(db, library_file_id=library_file_id, product_ids=sorted(desired))


async def _new_order(db: AsyncSession, *, name: str, lines: list[tuple[int, int]]) -> Project:
    """An active order with ``(product_id, quantity)`` lines. Lines are appended
    BEFORE the flush, like ``routes/projects.py::create_project`` does — the
    cascade fills ``project_id`` and no lazy load is ever touched."""
    project = Project(name=name, status="active", priority="normal")
    for i, (product_id, quantity) in enumerate(lines):
        project.lines.append(ProjectLine(product_id=product_id, quantity=quantity, sort_order=i))
    db.add(project)
    await db.flush()
    return project


async def create_job_order(db: AsyncSession, *, name: str, file_ids: list[int], targets: dict[str, int]) -> Project:
    """The wizard's shape (spec Decision 4): one ``adhoc_job`` product over every
    selected file; the kit is ``gcd`` of the positive targets — the line's
    quantity — and a targeted part keeps ``target // gcd``; untargeted parts
    are zeroed ("do not measure"). Raises before anything is written when the
    targets are empty; raises after the syncs (inside the caller's transaction,
    so nothing survives) when a key names no part."""
    files = await _active_files(db, file_ids)
    for f in files:
        if not is_plan_eligible(f.file_type):
            raise NotPlannable(f.id)
    positive = {key: n for key, n in targets.items() if n > 0}
    if not positive:
        raise NoTargets()
    product = Product(name=name, origin=ProductOrigin.ADHOC_JOB.value)
    db.add(product)
    await db.flush()
    for f in files:
        await _link(db, f.id, product.id)
    parts = (await db.execute(select(ProductPart).where(ProductPart.product_id == product.id))).scalars().all()
    unknown = sorted(positive.keys() - {part.name_key for part in parts})
    if unknown:
        raise UnknownPartKey(unknown[0])
    n = reduce(gcd, positive.values())
    for part in parts:
        target = positive.get(part.name_key, 0)
        part.qty_per_unit = target // n
        if target:
            part.auto = False  # the operator's number, not the seed's
    await db.flush()
    return await _new_order(db, name=name, lines=[(product.id, n)])


async def create_catalog_order(
    db: AsyncSession, *, name: str, product_id: int, file_ids: list[int], quantity: int
) -> Project:
    """The wizard's shape when the preview found the one catalogue product that
    links every file: an order of ``quantity`` units, the product untouched.
    Re-checked here — the client's word that the files are linked is not enough."""
    product = await db.get(Product, product_id)
    if product is None or product.origin != ProductOrigin.CATALOG.value:
        raise NotACatalogProduct()
    linked = set(
        (await db.execute(select(product_files.c.library_file_id).where(product_files.c.product_id == product_id)))
        .scalars()
        .all()
    )
    if not set(file_ids) <= linked:
        raise FilesNotLinked()
    return await _new_order(db, name=name, lines=[(product.id, quantity)])


async def _plate_product(db: AsyncSession, library_file_id: int, plate_index: int) -> Product | None:
    return (
        await db.execute(
            select(Product).where(
                Product.origin == ProductOrigin.ADHOC_PLATE.value,
                Product.origin_file_id == library_file_id,
                Product.origin_plate_index == plate_index,
            )
        )
    ).scalar_one_or_none()


async def find_or_create_plate_product(db: AsyncSession, *, file: LibraryFile, plate_index: int, stem: str) -> Product:
    """The ``adhoc_plate`` product for (file, plate), created on first use.

    Two dialogs can race to create the same one: the partial unique index makes
    the second INSERT fail, the SAVEPOINT rolls just that back, and the loser
    re-reads the winner's row.
    """
    existing = await _plate_product(db, file.id, plate_index)
    if existing is not None:
        return existing
    product = Product(
        name=stem if plate_index == 0 else f"{stem} · plate {plate_index}",
        origin=ProductOrigin.ADHOC_PLATE.value,
        origin_file_id=file.id,
        origin_plate_index=plate_index,
    )
    try:
        async with db.begin_nested():
            db.add(product)
            await db.flush()
    except IntegrityError:
        winner = await _plate_product(db, file.id, plate_index)
        if winner is None:  # pragma: no cover — the index just fired, so the row exists
            raise
        return winner
    await _link(db, file.id, product.id)
    return product


async def create_plates_order(
    db: AsyncSession, *, library_file_id: int, plates: list[tuple[int, int]], name: str | None
) -> Project:
    """The print dialog's shape (spec Decision 4): one line per plate, each on
    its plate product, ``quantity = copies``. The plate index is normalised the
    way the sync numbers plates — a single-plate file (or one without plate
    metadata) is plate 0 whatever the dialog said, so the slicer's ``1`` and the
    sync's ``0`` never make two products for one plate."""
    files = await _active_files(db, [library_file_id])
    file = files[0]
    if not is_plan_eligible(file.file_type):
        raise NotPlannable(file.id)
    wanted = wanted_plate_indices(file.file_metadata)
    copies_by_plate: dict[int, int] = {}
    for plate_index, copies in plates:
        idx = 0 if wanted == {0} else plate_index
        if idx != 0 and idx not in wanted:
            raise PlateNotFound(plate_index)
        if idx in copies_by_plate:
            raise DuplicatePlate(plate_index)
        copies_by_plate[idx] = copies
    stem = file_stem(file.filename)
    lines: list[tuple[int, int]] = []
    for idx, copies in copies_by_plate.items():
        product = await find_or_create_plate_product(db, file=file, plate_index=idx, stem=stem)
        lines.append((product.id, copies))
    if not name:
        name = (
            f"{stem} ×{next(iter(copies_by_plate.values()))}"
            if len(copies_by_plate) == 1
            else f"{stem} · {len(copies_by_plate)} plates"
        )
    return await _new_order(db, name=name, lines=lines)
