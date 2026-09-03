"""Filling a product's card from a 3MF, and the all-time printed count.

Two jobs that share nothing but a home: :func:`fill_from_file` copies what a
designer put inside a 3MF onto a product, and :func:`units_printed_total`
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

⚠️ **Nothing here writes into the 3MF.** A library file is the operator's
original; the card lives in the database (spec §Risks). ``update_metadata`` is
the archive's method and is not called from this module.
"""

import asyncio
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.project_line import ProjectLine
from backend.app.schemas.product import CardNote
from backend.app.services.order_metrics import attribute, load_order_context
from backend.app.services.product_files import (
    ATTACHMENT_CATEGORIES,
    CATEGORY_EXTENSIONS,
    attachment_limit,
    exceeds_attachment_limit,
    product_attachments_dir,
)
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
