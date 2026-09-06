"""Orders from files (spec 2026-09-06): the parts preview and the three ways a
product + order come out of library files without anybody authoring them.

Nothing here knows the operator's language: every refusal is an exception
class, and the ROUTE turns it into the English sentence the catalogue
translates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.models.library import LibraryFile
from backend.app.models.product import Product, ProductOrigin, ProductPlate, product_files
from backend.app.services.product_composition import estimate_seconds, plate_key_counts, recipe_for
from backend.app.services.product_sync import is_plan_eligible, wanted_plate_indices


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
