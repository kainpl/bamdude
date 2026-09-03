"""Filling a product's card from a 3MF, moving one between farms, and the
all-time printed count.

Jobs that share nothing but a home: :func:`fill_from_file` copies what a
designer put inside a 3MF onto a product, :func:`export_zip` / :func:`import_zip`
move a whole product between two BamDudes, and :func:`units_printed_total`
answers "how many of this thing have we ever made".

The rules :func:`fill_from_file` exists to enforce (spec §Decisions 2):

* **It never overwrites.** A column is written only when it is NULL or blank.
  An operator who typed "Me" into *designer* means it, and the only way to get
  the file's value back is to clear the field and re-read. The one field with an
  extra rule is the NAME: BambuStudio stamps ``Exported 3D Model`` on everything
  it exports, so a placeholder title is refused and the caller's own name (the
  filename stem) stands — see :data:`PLACEHOLDER_TITLES` and :func:`usable_title`.
* **It imports the designer's folders, and nothing else.** Only the entries the
  card parser listed under ``Auxiliaries/`` are ever copied, and only into a
  category whose allowlist accepts the extension. A 3MF is a ZIP an operator was
  handed: the mesh, the sliced G-code and an ``.exe`` dropped in ``Others/``
  must all stay out of the attachments directory, and the allowlists are the
  only thing standing between them and it (spec §Risks).
* **A re-read replaces only its own import.** ``replace_3mf_attachments`` drops
  the entries (and files) this SAME file produced last time, so re-reading twice
  does not leave two copies of every picture — and it leaves manual uploads, and
  imports from a different linked file, exactly where they are.

The rules :func:`export_zip` / :func:`import_zip` exist to enforce (spec
§Decisions 6):

* **The file hash is the identity, the filename is not.** An import asks the
  library which row a given SHA-256 should become and links THAT row; only
  content nobody holds is ingested. Two farms that both have the designer's
  3MF end up sharing one row apiece, not two copies each.
* **The library ingests, this module never does.** A file goes in through
  ``store_library_upload`` — the same function the upload route calls — so
  hashing, dedup, thumbnails, tags and folder inheritance are the library's
  answer and not a second one.
* **The pivots belong to the sync.** Files join the product through
  ``sync_product_for_file``, never by inserting ``product_files``, and always as
  a UNION with whatever products the reused row already belongs to.

⚠️ **Nothing here writes into the 3MF.** A library file is the operator's
original; the card lives in the database (spec §Risks). ``update_metadata`` is
the archive's method and is not called from this module.
"""

import asyncio
import hashlib
import io
import json
import logging
import os
import posixpath
import re
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.product import Product, ProductPart, ProductPlate, product_files
from backend.app.models.project_line import ProjectLine
from backend.app.schemas.product import CardNote
from backend.app.services.library_ingest import find_reusable_row
from backend.app.services.order_metrics import attribute, load_order_context
from backend.app.services.part_names import canonicalize, name_key
from backend.app.services.product_composition import purchased_name_key
from backend.app.services.product_files import (
    ATTACHMENT_CATEGORIES,
    CATEGORY_EXTENSIONS,
    COVER_EXTENSIONS,
    attachment_entry,
    attachment_limit,
    exceeds_attachment_limit,
    product_attachments_dir,
    safe_attachment_name,
    sorted_attachments,
)
from backend.app.services.product_sync import sync_product_for_file
from backend.app.services.threemf_card import CardData, ThreeMFCardParser

logger = logging.getLogger(__name__)

# BambuStudio's own defaults. A farm whose products are all called "Exported 3D
# Model" is worse off than one that kept the filenames, so these never become a
# product name — the rest of the card is still filled from such a file.
PLACEHOLDER_TITLES = {"Exported 3D Model", "Untitled"}

# card attribute → product column. ``source_url`` is deliberately absent: a 3MF
# carries no URL, only ``DesignModelId``, and inventing a MakerWorld link from
# it would be a guess stored as a fact.
_CARD_TO_COLUMN: dict[str, str] = {
    "description": "description",
    "designer": "designer",
    "license": "license",
    "design_model_id": "design_id",
}

# The columns are VARCHAR on PostgreSQL, which rejects an over-long value rather
# than truncating it the way SQLite does. A designer name from a hostile 3MF is
# not worth a 500 on the product page.
_COLUMN_LIMITS: dict[str, int] = {"name": 255, "designer": 255, "license": 255, "design_id": 64}


def resolve_disk_path(library_file: Any) -> Path | None:
    """The file's bytes, or ``None`` when the row outlives them.

    The import is function-local because ``to_absolute_path`` still lives in
    ``routes/library.py`` and a service importing a route module is the wrong
    direction — the same reason the upload allowlists were moved OUT of
    ``routes/projects.py`` into ``product_files``. Until that helper moves too,
    this is the shape ``services/calibration_service.py`` and
    ``services/library_3mf_preview.py`` already use.
    """
    from backend.app.api.routes.library import to_absolute_path

    path = to_absolute_path(library_file.file_path)
    return path if path is not None and path.is_file() else None


async def read_card(library_file: Any) -> CardData | None:
    """The file's card, or ``None`` when its bytes are gone. Never raises.

    Off the event loop: ``parse`` inflates ``3D/3dmodel.model`` and regex-scans
    it, which for a real model is megabytes of CPU — a long walk holds neither
    the transaction nor the loop.
    """
    path = resolve_disk_path(library_file)
    return await asyncio.to_thread(ThreeMFCardParser(path).parse) if path is not None else None


def usable_title(card: CardData | None) -> str | None:
    """The card's ``Title`` when it can name a product — ``None`` otherwise.

    ``parse`` has already stripped and unescaped it, so what is left to reject is
    emptiness and the two BambuStudio placeholders.
    """
    title = (card.title or "").strip() if card is not None else ""
    return None if not title or title in PLACEHOLDER_TITLES else title[: _COLUMN_LIMITS["name"]]


def _blank(value: Any) -> bool:
    return not str(value or "").strip()


def _safe_stored_name(name: Any) -> str | None:
    """A stored attachment name we are willing to join onto a path.

    Every name this module writes is ``uuid4().hex`` plus an extension, so a row
    that fails this came from somewhere else (a hand-edited JSON column, a
    restored backup) and its file is left alone rather than guessed at.
    """
    if not isinstance(name, str) or not name or "/" in name or "\\" in name or ".." in name:
        return None
    return name


def _note(code: str, **params: str | int) -> CardNote:
    return CardNote(code=code, params=params)


def _write_member(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _unlink_quietly(target: Path) -> None:
    if not target.exists():
        return
    try:
        target.unlink()
    except OSError as e:
        logger.warning("Failed to delete a replaced card attachment %s: %s", target, e)


async def fill_from_file(
    db: AsyncSession,
    product: Any,
    library_file: Any,
    *,
    replace_3mf_attachments: bool,
    card: CardData | None = None,
) -> list[CardNote]:
    """Fill the product's blank card fields from the file and import its auxiliaries.

    ``card`` lets a caller that has already parsed the file (``from-file``, which
    needs the title before the product exists) hand the parse in rather than
    opening the ZIP twice.

    ``db`` is used for exactly one thing, and it is the reason it is in the
    signature: the attachments JSON is FLUSHED before a single replaced file is
    unlinked. Deleting first and writing after would let a failure in between
    leave the column naming files that are no longer there — a product page full
    of broken pictures nothing can repair. This way the worst case is an orphan
    file on disk, which ``scripts/prune_orphan_archive_files.py`` reconciles and
    nobody sees. (The residual window is the commit itself, which this layer
    cannot reach — ``get_db`` owns it.)

    Returns structured notes, never prose: the operator reads them in their own
    language and only the frontend knows which that is.
    """
    notes: list[CardNote] = []
    path = resolve_disk_path(library_file)
    if path is None:
        return [_note("file_missing")]
    parser = ThreeMFCardParser(path)
    if card is None:
        card = await asyncio.to_thread(parser.parse)
    if card.error:
        return [_note("unreadable", error=card.error)]

    # ---- fields: blank only, never an overwrite ----
    if _blank(product.name):
        title = usable_title(card)
        if title:
            product.name = title
            notes.append(_note("filled_field", field="name"))
    for attribute_name, column in _CARD_TO_COLUMN.items():
        value = getattr(card, attribute_name, None)
        if not value or not _blank(getattr(product, column, None)):
            continue
        limit = _COLUMN_LIMITS.get(column)
        setattr(product, column, value[:limit] if limit else value)
        notes.append(_note("filled_field", field=column))

    # ---- attachments ----
    # ⚠️ ``Product.attachments`` is a plain JSON column: every writer ASSIGNS
    # a new list, because mutating the loaded one in place is invisible to the flush.
    rows = [dict(a) for a in (product.attachments or []) if isinstance(a, dict) and a.get("filename")]
    directory = product_attachments_dir(product.id)
    stale: list[Path] = []

    if replace_3mf_attachments:
        # Keyed on the stored name, which is a uuid and therefore unique — two
        # entries that happen to hold equal dicts must not delete each other.
        mine = {
            a["filename"]: a for a in rows if a.get("source") == "3mf" and a.get("source_file_id") == library_file.id
        }
        rows = [a for a in rows if a["filename"] not in mine]
        for entry in mine.values():
            stored = _safe_stored_name(entry.get("filename"))
            if stored is None:
                continue
            if product.cover_image_filename == stored:
                # The cover pointed at a picture that is about to go; leaving the
                # column would be a dangling reference for someone else to heal.
                product.cover_image_filename = None
            stale.append(directory / stored)  # SEC-PATH-OK: guarded by _safe_stored_name just above
        if mine:
            notes.append(_note("replaced_files", count=len(mine)))

    # Keyed on (category, name), not the name alone: one 3MF legitimately ships
    # ``guide.png`` in ``Model Pictures/`` AND in ``Assembly Guide/``, and those
    # are two different files that both belong on the product.
    already = {(a.get("category"), a.get("original_name")) for a in rows if a.get("source_file_id") == library_file.id}
    cursor = {
        category: (
            max((a.get("sort_order") or 0) for a in rows if a.get("category") == category) + 1
            if any(a.get("category") == category for a in rows)
            else 0
        )
        for category in ATTACHMENT_CATEGORIES
    }
    imported: dict[str, int] = {}
    fresh: list[dict] = []

    for category in ATTACHMENT_CATEGORIES:
        # ⚠️ ``card.auxiliaries[category]`` — the parser's listing of that
        # ONE folder — is the only source of entries. Never a namelist walk: this
        # is what keeps the mesh and the sliced G-code out of the product.
        for entry in card.auxiliaries.get(category, []):
            if (category, entry.name) in already:
                continue
            ext = os.path.splitext(entry.name)[1].lower()
            if ext not in CATEGORY_EXTENSIONS[category]:
                notes.append(_note("skipped_extension", name=entry.name, ext=ext, category=category))
                continue
            # The ZIP's declared UNCOMPRESSED size, checked before a byte is
            # inflated — so this bounds the allocation instead of reporting it.
            if exceeds_attachment_limit(entry.size):
                notes.append(_note("skipped_too_large", name=entry.name, size=entry.size, limit=attachment_limit()))
                continue
            payload = await asyncio.to_thread(parser.read, entry.zip_path)
            if payload is None:
                notes.append(_note("skipped_unreadable", name=entry.name))
                continue
            data = payload[0]
            stored = f"{uuid.uuid4().hex}{ext}"
            target = (
                directory / stored
            )  # SEC-PATH-OK: stored = uuid4().hex + an extension validated against this category's allowlist just above
            try:
                await asyncio.to_thread(_write_member, target, data)
            except OSError as e:
                logger.error("Failed to import a card attachment %s: %s", target, e)
                notes.append(_note("skipped_unsaved", name=entry.name))
                continue
            fresh.append(
                {
                    "category": category,
                    "filename": stored,
                    "original_name": entry.name,
                    "size": len(data),
                    "sort_order": cursor[category],
                    "source": "3mf",
                    "source_file_id": library_file.id,
                    "uploaded_at": datetime.now().isoformat(),
                }
            )
            cursor[category] += 1
            imported[category] = imported.get(category, 0) + 1

    if fresh or replace_3mf_attachments:
        product.attachments = rows + fresh
    for category, count in imported.items():
        notes.append(_note("imported_files", category=category, count=count))

    if stale:
        # The column first, the disk after — see the docstring.
        await db.flush()
        for target in stale:
            await asyncio.to_thread(_unlink_quietly, target)

    return notes or [_note("nothing_to_fill")]


async def units_printed_total(db: AsyncSession, product_id: int) -> int:
    """How many units of this product every order has ever printed (spec §Decisions 7).

    Σ of ``units_printed`` over every line of this product, across every order —
    computed the way the order page computes it, through ``load_order_context``
    and ``attribute``, so the number on the product page and the number on the
    order can never disagree. Products are few and their orders are hundreds at
    most; pass 6's grouped-figures work may replace the loop.
    """
    project_ids = (
        (await db.execute(select(ProjectLine.project_id).where(ProjectLine.product_id == product_id).distinct()))
        .scalars()
        .all()
    )
    total = 0
    for project_id in project_ids:
        context = await load_order_context(db, project_id)
        if context is None:
            continue
        figures, _ = attribute(context)
        total += sum(figs.units_printed for figs in figures.values() if figs.product_id == product_id)
    return total


# ---------- export / import (spec §Decisions 6) ----------

EXPORT_FORMAT = 1

# The one manifest name, and the only two directories a member may live in.
_MANIFEST = "product.json"
_FILES_ROOT = "files"
_ATTACHMENTS_ROOT = "attachments"
# A dedicated cover is not a gallery entry, so it cannot go under one of the
# four categories. This pseudo-category is deliberately NOT in
# ``ATTACHMENT_CATEGORIES``, which is what makes the two cases distinguishable
# on the way back in.
_COVER_ROOT = "cover"

_SLUG_MAX = 40

# The import buffers the whole archive AND every member it copies, so the
# ceiling is a memory bound before it is a policy. Four attachments' worth of
# ceiling comfortably clears a product with a couple of 3MFs and a photo set;
# an archive above it is refused before a single member is inflated.
_IMPORT_SIZE_FACTOR = 4


def export_slug(product: Any) -> str:
    """The filename stem: the product's name, ASCII, lower-case, hyphenated.

    ASCII only on purpose — the stem lands in a ``Content-Disposition`` header,
    where anything else needs RFC 5987 encoding that not every client reads. A
    name written entirely in another script therefore collapses to nothing, and
    the id names the file instead of a header nobody can parse.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (product.name or "").lower()).strip("-")[:_SLUG_MAX].strip("-")
    return slug or f"product-{product.id}"


def _member(directory: str, name: str, taken: set[str]) -> str:
    """A member path nothing else in the archive has.

    Two pictures may legitimately share an original name — the gallery keys on
    the stored uuid, not on what the operator called the file. In a ZIP they
    would be one member silently overwriting the other, so the second gets a
    ``(2)`` and the manifest records the name it actually went in as.
    """
    stem, ext = os.path.splitext(name)
    candidate = f"{directory}/{name}"
    n = 2
    while candidate in taken:
        candidate = f"{directory}/{stem} ({n}){ext}"
        n += 1
    taken.add(candidate)
    return candidate


async def _files_to_export(db: AsyncSession, product: Any) -> list[LibraryFile]:
    """Every file the product prints from: its direct links plus the children of
    its linked folders — which is what a folder link MEANS (the child pivot is
    the sync's business, but a file dropped into the folder after the link
    belongs to the product just as much as one the sync has already seen)."""
    ids = {f.id for f in product.library_files}
    folder_ids = [f.id for f in product.library_folders]
    if folder_ids:
        ids |= set(
            (await db.execute(select(LibraryFile.id).where(LibraryFile.folder_id.in_(folder_ids)))).scalars().all()
        )
    if not ids:
        return []
    # ``active()``: a trashed file is restorable and keeps its links, but it is
    # not part of what this product is today.
    rows = (await db.execute(LibraryFile.active().where(LibraryFile.id.in_(ids)))).scalars().all()
    return sorted(rows, key=lambda r: r.id)


def _part_manifest(part: Any) -> dict:
    return {
        "kind": part.kind,
        "name": part.name,
        "name_key": part.name_key,
        "qty_per_unit": part.qty_per_unit,
        "aliases": list(part.aliases) if part.aliases is not None else None,
        "auto": bool(part.auto),
        "unit_price": part.unit_price,
        "sourcing_url": part.sourcing_url,
        "remarks": part.remarks,
        "sort_order": part.sort_order,
    }


async def export_zip(db: AsyncSession, product: Any) -> tuple[bytes, str]:
    """The product as a ZIP, and the filename to offer it under.

    ⚠️ The whole archive is built in memory, so the export is bounded by what
    the product's files weigh. That is the same trade the attachment routes
    already make, and the alternative — a temp file the route streams and then
    has to delete on every exit path, cancellation included — buys nothing until
    somebody exports a gigabyte.

    A file whose row outlived its bytes is left out rather than failing the
    export: the operator asked for what this product IS, and one unreachable
    mount is not a reason to hand them nothing. Its plates drop out with it, so
    the manifest never promises a plate whose file it did not carry.
    """
    payload: dict[str, bytes] = {}
    taken: set[str] = set()
    hash_by_file: dict[int, str] = {}
    name_by_file: dict[int, str] = {}
    files: list[dict] = []
    seen_hashes: set[str] = set()

    for row in await _files_to_export(db, product):
        path = resolve_disk_path(row)
        if path is None:
            logger.info("Export of product %s skips file %s: its bytes are gone", product.id, row.id)
            continue
        data = await asyncio.to_thread(path.read_bytes)
        digest = hashlib.sha256(data).hexdigest()
        filename = os.path.basename(row.filename or f"{digest[:12]}.3mf")
        hash_by_file[row.id] = digest
        name_by_file[row.id] = filename
        if digest in seen_hashes:
            continue  # two rows, one content: the archive carries the bytes once
        seen_hashes.add(digest)
        member = _member(_FILES_ROOT, f"{digest}_{filename}", taken)
        payload[member] = data
        files.append({"hash": digest, "filename": filename, "size": len(data), "member": member})

    plates = [
        {
            "file_hash": hash_by_file[plate.library_file_id],
            "filename": name_by_file[plate.library_file_id],
            "plate_index": plate.plate_index,
        }
        for plate in sorted(product.plates, key=lambda p: (p.library_file_id, p.plate_index))
        if plate.library_file_id in hash_by_file
    ]

    directory = product_attachments_dir(product.id)
    attachments: list[dict] = []
    exported_name: dict[str, str] = {}
    for entry in sorted_attachments(product):
        stored = _safe_stored_name(entry.get("filename"))
        category = entry.get("category")
        if stored is None or category not in ATTACHMENT_CATEGORIES:
            continue
        source = directory / stored  # SEC-PATH-OK: guarded by _safe_stored_name just above
        if not source.is_file():
            continue
        member = _member(
            f"{_ATTACHMENTS_ROOT}/{category}", os.path.basename(str(entry.get("original_name") or stored)), taken
        )
        payload[member] = await asyncio.to_thread(source.read_bytes)
        original = posixpath.basename(member)
        exported_name[stored] = original
        attachments.append(
            {
                "category": category,
                "original_name": original,
                "sort_order": entry.get("sort_order") or 0,
                "source": entry.get("source") or "manual",
                "member": member,
            }
        )

    # The cover, in its two shapes. A picked gallery picture travels as the name
    # its own entry travelled under; a dedicated upload has no original name to
    # keep (the column is all there ever was), so it is given a readable one and
    # a directory of its own so the import can tell the two apart.
    cover: str | None = None
    explicit = _safe_stored_name(product.cover_image_filename)
    if explicit is not None:
        if explicit in exported_name:
            cover = exported_name[explicit]
        elif attachment_entry(product, explicit) is None:
            source = directory / explicit  # SEC-PATH-OK: guarded by _safe_stored_name just above
            if source.is_file():
                cover = f"{_COVER_ROOT}{os.path.splitext(explicit)[1].lower()}"
                payload[f"{_ATTACHMENTS_ROOT}/{_COVER_ROOT}/{cover}"] = await asyncio.to_thread(source.read_bytes)

    manifest = {
        "format": EXPORT_FORMAT,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "card": {
            "name": product.name,
            "description": product.description,
            "notes": product.notes,
            "designer": product.designer,
            "license": product.license,
            "source_url": product.source_url,
            "design_id": product.design_id,
        },
        "parts": [_part_manifest(p) for p in sorted(product.parts, key=lambda p: (p.sort_order, p.id))],
        "files": files,
        "plates": plates,
        "attachments": attachments,
        "cover": cover,
    }

    def _build() -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(_MANIFEST, json.dumps(manifest, indent=2, ensure_ascii=False))
            for name, data in payload.items():
                zf.writestr(name, data)
        return buf.getvalue()

    return await asyncio.to_thread(_build), (
        f"{export_slug(product)}_{datetime.now(timezone.utc).date().isoformat()}.zip"
    )


def _reject_hostile_members(zf: zipfile.ZipFile) -> None:
    """Every member name, checked BEFORE a byte is inflated or written.

    ⚠️ The archive is somebody else's file. ``ZipFile.extract`` sanitises names;
    this code never calls it — it reads members by name and writes to paths IT
    chooses — so the guard is here instead, and it is a whitelist: a member is
    the manifest or lives under one of two directories, full stop. Anything that
    is not already a normalised relative path under one of them is refused
    rather than repaired, because a name that needs repairing is a name whose
    author meant something by it.
    """
    total = 0
    ceiling = attachment_limit() * _IMPORT_SIZE_FACTOR
    for info in zf.infolist():
        name = info.filename
        if name.endswith("/"):
            continue  # a directory entry carries nothing
        root = name.split("/", 1)[0]
        if (
            "\\" in name
            or name.startswith("/")
            or ":" in root
            or posixpath.normpath(name) != name
            or not (name == _MANIFEST or root in (_FILES_ROOT, _ATTACHMENTS_ROOT))
        ):
            raise HTTPException(status_code=400, detail=f"The archive carries an illegal member name: {name!r}")
        total += info.file_size
        if total > ceiling:
            raise HTTPException(status_code=413, detail=f"An import may unpack to at most {ceiling} bytes")


def _validated_manifest(zf: zipfile.ZipFile) -> dict:
    """``product.json``, or a 400 saying which way it is wrong."""
    try:
        raw = zf.read(_MANIFEST)
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"The archive carries no {_MANIFEST}") from e
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"{_MANIFEST} is not valid JSON") from e
    if not isinstance(manifest, dict):
        raise HTTPException(status_code=400, detail=f"{_MANIFEST} must be a JSON object")
    if manifest.get("format") != EXPORT_FORMAT:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported export format {manifest.get('format')!r}; this BamDude reads format {EXPORT_FORMAT}",
        )
    card = manifest.get("card")
    if not isinstance(card, dict) or not str(card.get("name") or "").strip():
        raise HTTPException(status_code=400, detail=f"{_MANIFEST} carries no product name")
    for key in ("parts", "files", "plates", "attachments"):
        if not isinstance(manifest.get(key, []), list):
            raise HTTPException(status_code=400, detail=f"{_MANIFEST}: '{key}' must be a list")
    return manifest


def _text(value: Any, limit: int | None = None) -> str | None:
    """A manifest value we are willing to put in a column: a string, or nothing.

    A number, a list or a nested object in a text field is a manifest somebody
    edited; taking ``str()`` of it would store ``"{'a': 1}"`` as a designer's
    name. Length is enforced here because PostgreSQL refuses an over-long
    VARCHAR rather than truncating it the way SQLite does.
    """
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit] if limit else text


def _whole(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


async def _products_of_file(db: AsyncSession, library_file_id: int) -> set[int]:
    """Who else owns this file, read off the pivot.

    The sync takes a FULL desired set, never a delta, so an import that reuses
    an existing row must ask this first — handing it the new product alone would
    evict every current owner and delete their plates for the file.
    """
    return set(
        (await db.execute(select(product_files.c.product_id).where(product_files.c.library_file_id == library_file_id)))
        .scalars()
        .all()
    )


async def _import_destination(db: AsyncSession, folder_id: int | None, product_name: str) -> LibraryFolder:
    """Where a file nobody already has should land.

    ⚠️ A DESTINATION, not a link. The folder is never joined to the product:
    ``product_folders`` means "every file in here belongs to this product", and
    an operator who imports into their existing Downloads folder did not say
    that about the two hundred files already in it.

    ``selectinload``: ``store_library_upload`` runs ``inherit_folder_products``,
    which reads ``folder.products`` — a lazy load inside an async session is a
    ``MissingGreenlet``, not a SELECT.
    """
    if folder_id is not None:
        folder = (
            await db.execute(
                select(LibraryFolder).where(LibraryFolder.id == folder_id).options(selectinload(LibraryFolder.products))
            )
        ).scalar_one_or_none()
        if folder is None:
            raise HTTPException(status_code=404, detail="Folder not found")
        return folder
    folder = LibraryFolder(name=product_name[:255])
    db.add(folder)
    await db.flush()
    await db.refresh(folder, ["products"])
    return folder


async def import_zip(
    db: AsyncSession, zip_bytes: bytes, *, folder_id: int | None, user: Any
) -> tuple[Product, list[str]]:
    """Rebuild a product from an export. Returns it and everything it could not do.

    ⚠️ **The files go in first, on purpose.** ``store_library_upload`` commits —
    it is the library's own write path and always has been — so anything created
    before it is durable whether or not the rest of this function succeeds.
    Ingesting first means a failure half-way leaves library rows (which is
    exactly what an upload leaves) and NO half-built product, instead of a
    committed product with no files that nothing can tell apart from a real one.

    ⚠️ Warnings are not errors. A skipped attachment, or a plate the file no
    longer carries, must not cost the operator the other 95% of the import, so
    they ride back beside the product and the page shows them.
    """
    warnings: list[str] = []
    try:
        archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as e:
        raise HTTPException(status_code=400, detail="The uploaded file is not a ZIP archive") from e

    with archive as zf:
        # Both of these raise before anything is created. Nothing below may run
        # on an archive that has not passed them.
        _reject_hostile_members(zf)
        manifest = _validated_manifest(zf)
        names = set(zf.namelist())
        card = manifest["card"]
        product_name = _text(card.get("name"), 255) or "Imported product"

        # ---- the files, through the library's own door ----
        destination: LibraryFolder | None = None
        resolved = False
        if folder_id is not None:  # a 404 for a folder that is not there, before any write
            destination = await _import_destination(db, folder_id, product_name)
            resolved = True

        file_ids: list[int] = []
        hash_by_file: dict[int, str] = {}
        for entry in manifest.get("files") or []:
            if not isinstance(entry, dict):
                continue
            member = str(entry.get("member") or f"{_FILES_ROOT}/{entry.get('hash')}_{entry.get('filename')}")
            filename = os.path.basename(_text(entry.get("filename")) or "")
            if member not in names:
                warnings.append(f"{filename or member}: the archive carries no bytes for this file")
                continue
            data = zf.read(member)
            digest = hashlib.sha256(data).hexdigest()
            # ⚠️ The digest is COMPUTED, never read out of the manifest — the
            # manifest is the sender's word and the library row it picks is a
            # real file somebody else's product may already be printing.
            reusable = await find_reusable_row(db, content_hash=digest)
            if reusable is not None and reusable[1]:
                row = reusable[0]
            else:
                if not resolved:
                    destination = await _import_destination(db, None, product_name)
                    resolved = True
                try:
                    row = (
                        await _ingest_into_library(
                            db,
                            filename=filename or f"{digest[:12]}.3mf",
                            content=data,
                            target_folder=destination,
                            user=user,
                        )
                    ).file
                except HTTPException as e:
                    # 400 is the library saying THIS FILE is unacceptable — one
                    # bad member must not cost the whole import. Anything else
                    # (a read-only destination, a folder that vanished) is about
                    # the request, not the file, and belongs to the caller.
                    if e.status_code != 400:
                        raise
                    warnings.append(f"{filename or member}: the library refused this file ({e.detail})")
                    continue
            file_ids.append(row.id)
            hash_by_file[row.id] = digest

        # ---- the product ----
        product = Product(
            name=product_name,
            description=_text(card.get("description")),
            notes=_text(card.get("notes")),
            designer=_text(card.get("designer"), 255),
            license=_text(card.get("license"), 255),
            source_url=_text(card.get("source_url"), 2048),
            design_id=_text(card.get("design_id"), 64),
        )
        db.add(product)
        await db.flush()

        # ---- parts, BEFORE the sync ----
        # ``seed_parts_for_product`` creates a part for every object key no
        # existing part covers. Planting the manifest's parts first is what makes
        # the sync a no-op for them, so the operator's own quantities, aliases
        # and purchased rows survive instead of a fresh set of auto guesses.
        seen: set[str] = set()
        for position, raw in enumerate(manifest.get("parts") or []):
            if not isinstance(raw, dict):
                continue
            name = _text(raw.get("name"), 512)
            if not name:
                continue
            kind = "purchased" if raw.get("kind") == "purchased" else "printed"
            key = _text(raw.get("name_key"), 512) or (
                purchased_name_key(name) if kind == "purchased" else name_key(canonicalize(name))
            )
            if key in seen:
                # ``uq_product_parts_key`` would raise at flush time, far from here.
                warnings.append(f"{name}: another part of this product already answers to '{key}'")
                continue
            seen.add(key)
            aliases = [a for a in (raw.get("aliases") or []) if isinstance(a, str) and a] if kind == "printed" else None
            db.add(
                ProductPart(
                    product_id=product.id,
                    kind=kind,
                    name=name,
                    name_key=key,
                    qty_per_unit=max(0, _whole(raw.get("qty_per_unit"), 1)),
                    aliases=aliases,
                    auto=bool(raw.get("auto", False)),
                    unit_price=raw.get("unit_price") if isinstance(raw.get("unit_price"), (int, float)) else None,
                    sourcing_url=_text(raw.get("sourcing_url"), 512),
                    remarks=_text(raw.get("remarks")),
                    sort_order=_whole(raw.get("sort_order"), position),
                )
            )
        await db.flush()

        # ---- the links, through the one door that owns them ----
        for library_file_id in file_ids:
            await sync_product_for_file(
                db,
                library_file_id=library_file_id,
                product_ids=sorted(await _products_of_file(db, library_file_id) | {product.id}),
            )

        # ---- attachments and the cover ----
        rows, cover_stored = await _import_attachments(zf, names, product, manifest, warnings)
        product.attachments = rows
        if cover_stored:
            product.cover_image_filename = cover_stored
        await db.flush()

        # ---- what the manifest promised and the files no longer carry ----
        carried = set(hash_by_file.values())
        have = {
            (hash_by_file.get(library_file_id), plate_index)
            for library_file_id, plate_index in (
                await db.execute(
                    select(ProductPlate.library_file_id, ProductPlate.plate_index).where(
                        ProductPlate.product_id == product.id
                    )
                )
            ).all()
        }
        for raw in manifest.get("plates") or []:
            if not isinstance(raw, dict) or raw.get("file_hash") not in carried:
                continue  # its file never made it in, and that was already reported
            if (raw.get("file_hash"), raw.get("plate_index")) not in have:
                warnings.append(f"{raw.get('filename')}: plate {raw.get('plate_index')} is not in the file any more")

    return product, warnings


async def _ingest_into_library(db: AsyncSession, *, filename: str, content: bytes, target_folder: Any, user: Any):
    """The library's own upload path, called with the import's bytes.

    The import function-local ``store_library_upload`` import is the same
    concession :func:`resolve_disk_path` makes above: the helper still lives in
    ``routes/library.py`` because it leans on a dozen module-private helpers of
    that route, and a service importing a route module is the smaller wrong
    until they move. Calling anything else here would be a SECOND answer to
    "which row do these bytes become", which is exactly what
    ``services/library_ingest`` exists to prevent.
    """
    from backend.app.api.routes.library import store_library_upload

    return await store_library_upload(
        db,
        filename=filename,
        content=content,
        target_folder=target_folder,
        created_by_id=getattr(user, "id", None),
    )


async def _import_attachments(
    zf: zipfile.ZipFile, names: set[str], product: Product, manifest: dict, warnings: list[str]
) -> tuple[list[dict], str | None]:
    """Copy the archive's attachments onto the product. Returns the JSON rows and
    the stored name of the cover, when one was restored.

    ⚠️ ``CATEGORY_EXTENSIONS[category]`` is the only defence against an
    executable landing in the attachments directory (spec §Risks), and an import
    is exactly the path that would otherwise walk around the upload route. The
    category is checked first so the lookup can never fall back to "anything",
    and the extension the allowlist approved is the extension the file is
    written with.
    """
    directory = product_attachments_dir(product.id)
    rows: list[dict] = []
    cover_name = _text(manifest.get("cover"))
    cover_stored: str | None = None

    for raw in manifest.get("attachments") or []:
        if not isinstance(raw, dict):
            continue
        category = raw.get("category")
        member = str(raw.get("member") or f"{_ATTACHMENTS_ROOT}/{category}/{raw.get('original_name')}")
        original = _text(raw.get("original_name")) or posixpath.basename(member)
        if category not in ATTACHMENT_CATEGORIES:
            warnings.append(f"{original}: '{category}' is not an attachment category")
            continue
        if member not in names:
            warnings.append(f"{original}: the archive carries no bytes for this attachment")
            continue
        try:
            safe_attachment_name(original)
        except HTTPException:
            warnings.append(f"{original}: not a name a file can be stored under")
            continue
        ext = os.path.splitext(original)[1].lower()
        if ext not in CATEGORY_EXTENSIONS[category]:
            warnings.append(f"{original}: '{ext or original}' is not allowed in {category}")
            continue
        if exceeds_attachment_limit(zf.getinfo(member).file_size):
            warnings.append(f"{original}: larger than the {attachment_limit()}-byte attachment ceiling")
            continue
        data = zf.read(member)
        stored = f"{uuid.uuid4().hex}{ext}"
        target = (
            directory / stored
        )  # SEC-PATH-OK: stored = uuid4().hex + an extension validated against this category's allowlist just above
        try:
            await asyncio.to_thread(_write_member, target, data)
        except OSError as e:
            logger.error("Failed to write an imported attachment %s: %s", target, e)
            warnings.append(f"{original}: could not be saved")
            continue
        rows.append(
            {
                "category": category,
                "filename": stored,
                "original_name": original,
                "size": len(data),
                "sort_order": _whole(raw.get("sort_order")),
                # ⚠️ ``source_file_id`` stays NULL: these entries did not come
                # from a 3MF THIS farm holds, so a later re-read of a linked file
                # must never mistake them for its own and replace them.
                "source": "import",
                "source_file_id": None,
                "uploaded_at": datetime.now().isoformat(),
            }
        )
        if cover_stored is None and cover_name and original == cover_name and category == "pictures":
            cover_stored = stored

    # A dedicated cover: its own directory, and it stays out of the gallery here
    # exactly as it did on the farm it came from.
    member = f"{_ATTACHMENTS_ROOT}/{_COVER_ROOT}/{cover_name}" if cover_name else None
    if cover_stored is not None or member is None:
        return rows, cover_stored
    if member not in names:
        warnings.append(f"{cover_name}: the cover image was not in the archive")
        return rows, None
    ext = os.path.splitext(cover_name)[1].lower()
    if ext not in COVER_EXTENSIONS:
        warnings.append(f"{cover_name}: '{ext or cover_name}' is not a cover image")
        return rows, None
    if exceeds_attachment_limit(zf.getinfo(member).file_size):
        warnings.append(f"{cover_name}: larger than the {attachment_limit()}-byte attachment ceiling")
        return rows, None
    stored = f"cover_{uuid.uuid4().hex}{ext}"
    target = (
        directory / stored
    )  # SEC-PATH-OK: 'cover_' + uuid4().hex + an extension validated against the cover allowlist just above
    try:
        await asyncio.to_thread(_write_member, target, zf.read(member))
    except OSError as e:
        logger.error("Failed to write an imported cover %s: %s", target, e)
        warnings.append(f"{cover_name}: could not be saved")
        return rows, None
    return rows, stored
