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
* **Neither direction is held in memory.** The export builds its ZIP on a temp
  file with ``ZipFile.write``, the import reads members out of a temp file the
  route streamed the upload into. A product linked to a folder of
  hundred-megabyte 3MFs is a normal product, and buffering one would put the
  farm's whole recipe on the heap of a Raspberry Pi.

⚠️ **Nothing here writes into the 3MF.** A library file is the operator's
original; the card lives in the database (spec §Risks). ``update_metadata`` is
the archive's method and is not called from this module.
"""

import asyncio
import hashlib
import json
import logging
import os
import posixpath
import re
import tempfile
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.product import Product, ProductPart, ProductPlate, product_files
from backend.app.schemas.product import CardNote
from backend.app.services.library_ingest import external_hash_is_stale, find_reusable_row
from backend.app.services.order_metrics import grouped_figures, units_delivered
from backend.app.services.part_names import canonicalize, name_key
from backend.app.services.product_composition import purchased_name_key
from backend.app.services.product_files import (
    ATTACHMENT_CATEGORIES,
    CATEGORY_EXTENSIONS,
    COVER_EXTENSIONS,
    SOURCE_3MF,
    SOURCE_IMPORT,
    SOURCE_MANUAL,
    attachment_limit,
    exceeds_attachment_limit,
    import_member_limit,
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


# ⚠️ **One ``to_thread`` per member, and it does ALL of the blocking work.**
# Inflating a member is CPU on a buffer that may be two hundred megabytes, and
# hashing it is another pass over the same bytes — on the event loop that is the
# whole farm's websockets, MQTT callbacks and every other request stopped for
# the duration. Reading on the loop and threading only the write (which is what
# the cover path used to do) buys nothing: the read is the expensive half.
#
# The ``ZipFile`` is created by :func:`import_zip` and handed to these helpers
# from ITS coroutine only, one call at a time — a ``ZipFile`` is not thread-safe
# (one shared file handle, one seek position), and this is safe precisely
# because the calls are sequential and the coroutine awaits each one before it
# makes the next. Never fan these out with ``gather``.
def _read_and_hash_member(zf: zipfile.ZipFile, name: str) -> tuple[bytes, str]:
    """A library member and the SHA-256 of what was actually inflated."""
    data = zf.read(name)
    return data, hashlib.sha256(data).hexdigest()


def _read_and_write_member(zf: zipfile.ZipFile, name: str, target: Path) -> bytes:
    """An attachment member, straight from the archive onto disk."""
    data = zf.read(name)
    _write_member(target, data)
    return data


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
                    "source": SOURCE_3MF,
                    "source_file_id": library_file.id,
                    # UTC and AWARE, like every other timestamp this codebase
                    # stores. A naive local stamp is unreadable the moment two
                    # farms exchange an export, and it is the one field here
                    # nothing can recompute afterwards.
                    "uploaded_at": datetime.now(timezone.utc).isoformat(),
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
    the same arithmetic the order page runs, so the number on the product page
    and the number on the order can never disagree.

    ⚠️ EVERY product endpoint answers through ``routes/products.py::_response``,
    so this runs on create, patch, duplicate, every part and link route, not
    only the detail GET. It used to load a full order context per order that
    ever printed the product; ``grouped_figures`` loads them all at once.
    """
    return units_delivered(await grouped_figures(db, product_ids=[product_id]), product_id)


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
_HASH_CHUNK = 1024 * 1024


@dataclass(slots=True)
class ExportArchive:
    """A finished export: the file on disk and the two names it goes out under.

    ``filename`` is the product's own name and travels in ``filename*``;
    ``ascii_filename`` is the slug and fills the legacy ``filename=`` parameter,
    because dropping the non-ASCII characters out of a Ukrainian product name
    leaves the date and nothing else.

    ⚠️ ``path`` is a TEMP FILE and the caller owns deleting it. The route hands
    that to a ``BackgroundTask`` so it happens once the bytes are on the wire.
    """

    path: Path
    filename: str
    ascii_filename: str


@dataclass(slots=True)
class _ImportedAttachments:
    """What an import has put in the attachments directory so far.

    The caller creates it, hands it to :func:`_import_attachments` and cleans up
    from it if anything after that raises — so it must name every file on disk,
    including the dedicated cover, which is not one of the gallery ``rows``.
    """

    rows: list[dict] = field(default_factory=list)
    cover: str | None = None

    def files_written(self) -> list[str]:
        names = [row["filename"] for row in self.rows if row.get("filename")]
        if self.cover and self.cover not in names:
            names.append(self.cover)
        return names


@dataclass(slots=True)
class _FileSpec:
    """One library file, flattened for the worker thread.

    ⚠️ Plain scalars, never the ORM row. The whole archive is built inside one
    ``to_thread``, and touching an unloaded or expired attribute off the event
    loop is a ``MissingGreenlet``, not a lazy SELECT — so everything the thread
    needs is read here, on the loop, first. The three names are the ones
    ``external_hash_is_stale`` asks for, so this duck-types as its ``row``.
    """

    library_file_id: int
    path: Path
    filename: str
    file_hash: str | None
    is_external: bool
    file_size: int | None
    fs_modified_at: Any


def export_slug(product: Any) -> str:
    """The ASCII fallback stem: the product's name, lower-case, hyphenated.

    ASCII only on purpose — this fills the legacy ``filename="..."`` parameter,
    which Starlette encodes as latin-1. A name written entirely in another
    script therefore collapses to nothing and the id names the file instead;
    the real name still reaches the browser through ``filename*``.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (product.name or "").lower()).strip("-")[:_SLUG_MAX].strip("-")
    return slug or f"product-{product.id}"


# Everything a member name may not carry: either path separator (``\`` is not
# one on POSIX, so a backslash SURVIVES ``os.path.basename`` there and lands in
# the archive verbatim), the C0/C1 control characters and DEL.
_UNSAFE_MEMBER_CHARS = re.compile(r"[/\\\x00-\x1f\x7f-\x9f]")


def _safe_member_name(name: Any) -> str:
    """The operator's own filename, made fit to be a ZIP member name.

    ⚠️ **The exporter's half of a rule the importer already enforces.**
    ``_reject_hostile_members`` refuses a whole archive whose member names carry
    a separator — including a backslash, which on POSIX is an ordinary character
    in a filename and therefore reaches the ZIP intact. An attachment somebody
    uploaded as ``foo\\bar.png`` then produced an export THIS BamDude answers 400
    to: a round trip that fails on our own output.

    Sanitised, not truncated to a basename: ``a/b.png`` becomes ``a_b.png``
    rather than ``b.png``, so the name the operator sees on the other side is
    still the name they gave. Leading dots go the same way — a member called
    ``.hidden`` is a hidden file wherever the archive is unpacked by hand, and
    ``..`` is the traversal the importer refuses outright.

    The importer's refusal STAYS. This is defence in depth on our own writes; it
    says nothing about the archives other farms send.
    """
    cleaned = _UNSAFE_MEMBER_CHARS.sub("_", str(name or ""))
    bare = cleaned.lstrip(".")
    cleaned = "_" * (len(cleaned) - len(bare)) + bare
    return cleaned or "_"


def _member(directory: str, name: str, taken: set[str]) -> str:
    """A member path nothing else in the archive has.

    Two pictures may legitimately share an original name — the gallery keys on
    the stored uuid, not on what the operator called the file. In a ZIP they
    would be one member silently overwriting the other, so the second gets a
    ``(2)`` and the manifest records the name it actually went in as.

    ⚠️ Every arcname the exporter writes goes through here, which is what makes
    :func:`_safe_member_name` unskippable — a second place that built a member
    path by hand would be the one that shipped a backslash.
    """
    name = _safe_member_name(name)
    stem, ext = os.path.splitext(name)
    candidate = f"{directory}/{name}"
    n = 2
    while candidate in taken:
        candidate = f"{directory}/{stem} ({n}){ext}"
        n += 1
    taken.add(candidate)
    return candidate


def _digest_of(spec: _FileSpec) -> str:
    """The file's SHA-256 — from the row when the row can be believed.

    A MANAGED file lives under BamDude's own directory: nothing outside changes
    it, so the hash written at ingest still describes it. An EXTERNAL file lives
    on somebody's mount and may have been replaced since the last scan, so its
    stored hash is trusted only while ``external_hash_is_stale`` says size and
    mtime still match. Everything else is read, in chunks — a 3MF is not a thing
    to hold in memory to hash it.
    """
    from backend.app.api.routes.library import _mtime_to_utc

    if spec.file_hash:
        if not spec.is_external:
            return spec.file_hash
        try:
            stat = spec.path.stat()
        except OSError:
            stat = None
        if stat is not None and not external_hash_is_stale(spec, size=stat.st_size, mtime=_mtime_to_utc(stat.st_mtime)):
            return spec.file_hash

    digest = hashlib.sha256()
    with spec.path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


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


def _write_export(
    specs: list[_FileSpec],
    plate_rows: list[tuple[int, int]],
    attachment_sources: list[tuple[dict, Path]],
    cover_source: tuple[str, Path] | None,
    card: dict,
    parts: list[dict],
) -> tuple[str, dict]:
    """Build the archive on disk and return ``(temp path, manifest)``.

    ⚠️ **Nothing is read into memory.** Every member goes in through
    ``ZipFile.write``, which streams the file straight through the compressor,
    so the export of a product linked to a folder of hundred-megabyte 3MFs costs
    a temp file and not the farm's RAM. That is also why ``product.json`` is
    written LAST: it names the hashes, and the hashes are only known once every
    file has been read once.

    One thread hop for the whole job — hashing, compressing and writing are all
    blocking, and splitting them would only multiply the hops.
    """
    handle, name = tempfile.mkstemp(prefix="bamdude-product-export-", suffix=".zip")
    os.close(handle)
    path = Path(name)
    taken: set[str] = set()
    hash_by_file: dict[int, str] = {}
    name_by_file: dict[int, str] = {}
    files: list[dict] = []
    members: dict[str, Path] = {}

    try:
        for spec in specs:
            digest = _digest_of(spec)
            hash_by_file[spec.library_file_id] = digest
            name_by_file[spec.library_file_id] = spec.filename
            if digest in {f["hash"] for f in files}:
                continue  # two rows, one content: the archive carries the bytes once
            member = _member(_FILES_ROOT, f"{digest}_{spec.filename}", taken)
            members[member] = spec.path
            files.append(
                {
                    "hash": digest,
                    "filename": spec.filename,
                    "size": spec.path.stat().st_size,
                    "member": member,
                }
            )

        attachments: list[dict] = []
        exported_name: dict[str, str] = {}
        for entry, source in attachment_sources:
            # No ``os.path.basename``: it splits on ``\`` on Windows and not on
            # POSIX, so the same product would export under two different member
            # names depending on the farm's OS. ``_safe_member_name`` (inside
            # ``_member``) neutralises both separators the same way everywhere.
            member = _member(f"{_ATTACHMENTS_ROOT}/{entry['category']}", str(entry["original_name"]), taken)
            members[member] = source
            exported_name[entry["filename"]] = posixpath.basename(member)
            attachments.append(
                {
                    "category": entry["category"],
                    "original_name": posixpath.basename(member),
                    "sort_order": entry.get("sort_order") or 0,
                    "source": entry.get("source") or SOURCE_MANUAL,
                    "member": member,
                }
            )

        cover: str | None = None
        if cover_source is not None:
            stored, source = cover_source
            if stored in exported_name:
                cover = exported_name[stored]
            else:
                cover = _safe_member_name(f"{_COVER_ROOT}{os.path.splitext(stored)[1].lower()}")
                members[f"{_ATTACHMENTS_ROOT}/{_COVER_ROOT}/{cover}"] = source

        manifest = {
            "format": EXPORT_FORMAT,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "card": card,
            "parts": parts,
            "files": files,
            "plates": [
                {
                    "file_hash": hash_by_file[library_file_id],
                    "filename": name_by_file[library_file_id],
                    "plate_index": plate_index,
                }
                for library_file_id, plate_index in plate_rows
                if library_file_id in hash_by_file
            ],
            "attachments": attachments,
            "cover": cover,
        }

        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for member, source in members.items():
                zf.write(source, arcname=member)
            zf.writestr(_MANIFEST, json.dumps(manifest, indent=2, ensure_ascii=False))
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return name, manifest


async def export_zip(db: AsyncSession, product: Any) -> ExportArchive:
    """The product as a ZIP on disk, and the names to offer it under.

    The archive is a temp file the CALLER deletes — see :class:`ExportArchive`.
    Nothing about it is buffered, so the export has no size ceiling of its own;
    the round trip is bounded on the other side instead, by
    ``product_files.import_limit()``, which is what a farm will accept back.

    A file whose row outlived its bytes is left out rather than failing the
    export: the operator asked for what this product IS, and one unreachable
    mount is not a reason to hand them nothing. Its plates drop out with it, so
    the manifest never promises a plate whose file it did not carry.
    """
    specs: list[_FileSpec] = []
    for row in await _files_to_export(db, product):
        path = resolve_disk_path(row)
        if path is None:
            logger.info("Export of product %s skips file %s: its bytes are gone", product.id, row.id)
            continue
        specs.append(
            _FileSpec(
                library_file_id=row.id,
                path=path,
                filename=os.path.basename(row.filename or f"file-{row.id}.3mf"),
                file_hash=row.file_hash,
                is_external=bool(row.is_external),
                file_size=row.file_size,
                fs_modified_at=row.fs_modified_at,
            )
        )

    directory = product_attachments_dir(product.id)
    attachment_sources: list[tuple[dict, Path]] = []
    for entry in sorted_attachments(product):
        stored = _safe_stored_name(entry.get("filename"))
        if stored is None or entry.get("category") not in ATTACHMENT_CATEGORIES:
            continue
        source = directory / stored  # SEC-PATH-OK: guarded by _safe_stored_name just above
        if source.is_file():
            attachment_sources.append(({**entry, "original_name": entry.get("original_name") or stored}, source))

    # The cover, in its two shapes. A picked gallery picture travels as the name
    # its own entry travelled under; a dedicated upload has no original name to
    # keep (the column is all there ever was), so the worker gives it a readable
    # one and a directory of its own so the import can tell the two apart.
    cover_source: tuple[str, Path] | None = None
    explicit = _safe_stored_name(product.cover_image_filename)
    if explicit is not None:
        source = directory / explicit  # SEC-PATH-OK: guarded by _safe_stored_name just above
        if source.is_file():
            cover_source = (explicit, source)

    path, _manifest_written = await asyncio.to_thread(
        _write_export,
        specs,
        [
            (plate.library_file_id, plate.plate_index)
            for plate in sorted(product.plates, key=lambda p: (p.library_file_id, p.plate_index))
        ],
        attachment_sources,
        cover_source,
        {
            "name": product.name,
            "description": product.description,
            "notes": product.notes,
            "designer": product.designer,
            "license": product.license,
            "source_url": product.source_url,
            "design_id": product.design_id,
        },
        [_part_manifest(p) for p in sorted(product.parts, key=lambda p: (p.sort_order, p.id))],
    )
    date = datetime.now(timezone.utc).date().isoformat()
    return ExportArchive(
        path=Path(path),
        filename=f"{product.name}_{date}.zip",
        ascii_filename=f"{export_slug(product)}_{date}.zip",
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

    There is deliberately no total-inflation ceiling here any more. The upload is
    streamed to disk against ``import_limit()`` before this runs, and the members
    are read one at a time against their own caps — a single number covering the
    sum of everything only ever refused legitimate exports of large products.
    """
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


def _price(value: Any) -> float | None:
    """A unit price, or nothing.

    ⚠️ ``bool`` is checked FIRST and rejected: it is a subclass of ``int``, so
    ``isinstance(True, (int, float))`` is True and a manifest carrying
    ``"unit_price": true`` would price the part at 1.00.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


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

    Without a ``folder_id`` an existing MANAGED ROOT folder of the same name is
    REUSED before a new one is made. Importing the same export twice — the
    ordinary way an operator retries — otherwise leaves two "Desk Lamp" folders
    side by side, and a third on the next attempt, with nothing to say which is
    which.

    ⚠️ **Managed only.** An external folder is a window onto somebody's mount,
    and ``_resolve_upload_destination`` WRITES THROUGH to it — so a share that
    happens to have a directory named after the product would have received the
    imported 3MFs, or, if it is read-only, answered 403. A 403 is not a 400, so
    it would not degrade to a warning either: it would abort the whole import
    from inside the ingest loop. The operator never asked for either; they asked
    for a product. Naming a mount explicitly through ``folder_id`` is still
    honoured, because then they did ask.

    ``selectinload``: ``store_library_upload`` runs ``inherit_folder_products``,
    which reads ``folder.products`` — a lazy load inside an async session is a
    ``MissingGreenlet``, not a SELECT.
    """
    query = select(LibraryFolder).options(selectinload(LibraryFolder.products))
    if folder_id is not None:
        folder = (await db.execute(query.where(LibraryFolder.id == folder_id))).scalar_one_or_none()
        if folder is None:
            raise HTTPException(status_code=404, detail="Folder not found")
        return folder
    existing = (
        await db.execute(
            query.where(
                LibraryFolder.name == product_name,
                LibraryFolder.parent_id.is_(None),
                LibraryFolder.is_external.is_(False),
            ).order_by(LibraryFolder.id)
        )
    ).scalars()
    folder = next(iter(existing), None)
    if folder is not None:
        return folder
    folder = LibraryFolder(name=product_name[:255])
    db.add(folder)
    await db.flush()
    await db.refresh(folder, ["products"])
    return folder


async def import_zip(
    db: AsyncSession, zip_source: Any, *, folder_id: int | None, user: Any
) -> tuple[Product, list[CardNote]]:
    """Rebuild a product from an export. Returns it and every note.

    ``zip_source`` is anything ``zipfile.ZipFile`` opens — a path, or the
    seekable file the multipart parser has ALREADY spooled the upload into.
    Never bytes: members are read out of it one at a time, so a two-gigabyte
    archive costs whatever the parser spent and not the farm's memory on top.
    The caller owns closing or deleting it.

    **The order, and the rule it buys.** The manifest and every member NAME are
    validated before a single write happens, so a hostile or unreadable archive
    creates nothing at all. After that, files are ingested BEFORE the product
    row exists — ``store_library_upload`` commits, so anything created before it
    is durable regardless of what follows, and library rows are exactly what an
    upload leaves behind anyway.

    ⚠️ **A file the library refuses is skipped, not fatal.** It comes back as
    ``import_file_refused`` carrying the library's own words, the product is
    still created, and the parts that file would have bound to simply stay
    unbound — a product with one unprintable member and a warning on screen is
    worth more to an operator than a 400 and nothing. Same for a skipped
    attachment or a plate the file no longer carries.
    """
    notes: list[CardNote] = []
    try:
        archive = zipfile.ZipFile(zip_source)
    except (zipfile.BadZipFile, OSError) as e:
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
                notes.append(_note("import_file_missing", name=filename or member))
                continue
            # ⚠️ The one place a library member is materialised whole:
            # ``store_library_upload`` takes ``content: bytes``. Teaching the
            # library ingest to take a stream is a change to every upload path
            # in the product and is deliberately out of this pass, so the size
            # is read off the ZIP directory and refused BEFORE anything is
            # inflated. A member over the cap is skipped like a refused file —
            # its parts simply stay unbound and the rest of the import lands.
            declared = zf.getinfo(member).file_size
            if declared > import_member_limit():
                notes.append(
                    _note(
                        "skipped_too_large",
                        name=filename or member,
                        size=declared,
                        limit=import_member_limit(),
                        category=_FILES_ROOT,
                    )
                )
                continue
            data, digest = await asyncio.to_thread(_read_and_hash_member, zf, member)
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
                    notes.append(_note("import_file_refused", name=filename or member, detail=str(e.detail)))
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
                notes.append(_note("import_part_duplicate_key", name=name, key=key))
                continue
            seen.add(key)
            aliases = [a for a in (raw.get("aliases") or []) if isinstance(a, str) and a] if kind == "printed" else None
            db.add(
                ProductPart(
                    product_id=product.id,
                    kind=kind,
                    name=name,
                    name_key=key,
                    # ⚠️ Floors at 0, not at 1. ``qty_per_unit = 0`` is the
                    # "present on a plate but not part of the product" rule the
                    # model documents; raising it to 1 here would invent a
                    # requirement the operator deliberately removed, on a round
                    # trip whose whole job is to change nothing.
                    qty_per_unit=max(0, _whole(raw.get("qty_per_unit"), 1)),
                    aliases=aliases,
                    auto=bool(raw.get("auto", False)),
                    unit_price=_price(raw.get("unit_price")),
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
        # ⚠️ The guard opens BEFORE the writing starts, not after it. ``zf.read``
        # raises on an encrypted member and on a bad CRC, so a failure INSIDE the
        # copy would otherwise leave every file it had already written orphaned —
        # nothing references them, and nothing sweeps them.
        directory = product_attachments_dir(product.id)
        written = _ImportedAttachments()
        try:
            await _import_attachments(zf, names, product, manifest, notes, written)
            product.attachments = written.rows
            if written.cover:
                product.cover_image_filename = written.cover
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
                filename = _text(raw.get("filename")) or "?"
                try:
                    plate_index = int(raw.get("plate_index"))
                except (TypeError, ValueError):
                    # An unreadable index cannot be checked against anything, so
                    # it is reported as missing rather than silently dropped.
                    notes.append(
                        _note("import_plate_missing", filename=filename, plate_index=str(raw.get("plate_index")))
                    )
                    continue
                if (raw.get("file_hash"), plate_index) not in have:
                    notes.append(_note("import_plate_missing", filename=filename, plate_index=plate_index))
        except BaseException:
            # The files are on disk and the column that would have named them is
            # about to be rolled back. Undo the writes — every one this run made,
            # whether or not it got as far as building a row for it — then let
            # the failure through unchanged.
            for stored in written.files_written():
                _unlink_quietly(directory / stored)  # SEC-PATH-OK: a name this module generated, uuid + a checked ext
            raise

    return product, notes


async def _ingest_into_library(db: AsyncSession, *, filename: str, content: bytes, target_folder: Any, user: Any):
    """The library's own upload path, called with the import's bytes.

    The function-local ``store_library_upload`` import is the same concession
    :func:`resolve_disk_path` makes above: the helper still lives in
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
    zf: zipfile.ZipFile,
    names: set[str],
    product: Product,
    manifest: dict,
    notes: list[CardNote],
    written: "_ImportedAttachments",
) -> None:
    """Copy the archive's attachments onto the product, into ``written``.

    ⚠️ An OUT-PARAMETER rather than a return value, and that is the point: this
    function writes files, and it can raise part-way through (``zf.read`` throws
    on an encrypted member and on a bad CRC). A caller that only learns what was
    written from a return it never receives cannot clean up after a failure. The
    accumulator is the caller's, so it holds whatever was written up to the
    moment things went wrong.

    ⚠️ ``CATEGORY_EXTENSIONS[category]`` is the only defence against an
    executable landing in the attachments directory (spec §Risks), and an import
    is exactly the path that would otherwise walk around the upload route. The
    category is checked first so the lookup can never fall back to "anything",
    and the extension the allowlist approved is the extension the file is
    written with.
    """
    directory = product_attachments_dir(product.id)
    rows = written.rows
    cover_name = _text(manifest.get("cover"))
    cover_stored: str | None = None

    for raw in manifest.get("attachments") or []:
        if not isinstance(raw, dict):
            continue
        category = raw.get("category")
        member = str(raw.get("member") or f"{_ATTACHMENTS_ROOT}/{category}/{raw.get('original_name')}")
        original = _text(raw.get("original_name")) or posixpath.basename(member)
        if category not in ATTACHMENT_CATEGORIES:
            notes.append(_note("import_bad_category", name=original, category=str(category)))
            continue
        if member not in names:
            notes.append(_note("import_attachment_missing", name=original))
            continue
        try:
            safe_attachment_name(original)
        except HTTPException:
            notes.append(_note("import_bad_name", name=original))
            continue
        ext = os.path.splitext(original)[1].lower()
        if ext not in CATEGORY_EXTENSIONS[category]:
            notes.append(_note("skipped_extension", name=original, ext=ext, category=category))
            continue
        declared = zf.getinfo(member).file_size
        if exceeds_attachment_limit(declared):
            notes.append(_note("skipped_too_large", name=original, size=declared, limit=attachment_limit()))
            continue
        stored = f"{uuid.uuid4().hex}{ext}"
        target = (
            directory / stored
        )  # SEC-PATH-OK: stored = uuid4().hex + an extension validated against this category's allowlist just above
        try:
            data = await asyncio.to_thread(_read_and_write_member, zf, member, target)
        except OSError as e:
            # ⚠️ A write that fails PART-WAY — the ordinary shape of ENOSPC —
            # leaves a truncated file behind, and no row will name it because
            # this entry is being skipped. Nothing else would ever remove it.
            logger.error("Failed to write an imported attachment %s: %s", target, e)
            _unlink_quietly(target)
            notes.append(_note("skipped_unsaved", name=original))
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
                "source": SOURCE_IMPORT,
                "source_file_id": None,
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        if cover_stored is None and cover_name and original == cover_name and category == "pictures":
            cover_stored = stored

    # A dedicated cover: its own directory, and it stays out of the gallery here
    # exactly as it did on the farm it came from.
    member = f"{_ATTACHMENTS_ROOT}/{_COVER_ROOT}/{cover_name}" if cover_name else None
    if cover_stored is not None:
        written.cover = cover_stored
        return
    if member is None:
        return
    if member not in names:
        notes.append(_note("import_cover_missing"))
        return
    ext = os.path.splitext(cover_name)[1].lower()
    if ext not in COVER_EXTENSIONS:
        notes.append(_note("skipped_extension", name=cover_name, ext=ext, category=_COVER_ROOT))
        return
    declared = zf.getinfo(member).file_size
    if exceeds_attachment_limit(declared):
        notes.append(
            _note("skipped_too_large", name=cover_name, size=declared, limit=attachment_limit(), category=_COVER_ROOT)
        )
        return
    stored = f"cover_{uuid.uuid4().hex}{ext}"
    target = (
        directory / stored
    )  # SEC-PATH-OK: 'cover_' + uuid4().hex + an extension validated against the cover allowlist just above
    # ⚠️ Recorded BEFORE the write, so a failure half-way through leaves a name
    # the caller's cleanup can act on rather than a file nobody knows about.
    written.cover = stored
    try:
        await asyncio.to_thread(_read_and_write_member, zf, member, target)
    except OSError as e:
        logger.error("Failed to write an imported cover %s: %s", target, e)
        notes.append(_note("skipped_unsaved", name=cover_name, category=_COVER_ROOT))
        written.cover = None
        _unlink_quietly(target)
