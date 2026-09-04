"""Library 3MFs that carry no object metadata get it — at upgrade and at boot.

``print_archive_parts`` is backfilled from ARCHIVE 3MFs by m158. A LIBRARY 3MF
whose ``file_metadata`` never got ``printable_objects`` — uploaded before the
extractor existed, or scanned while its mount was unreachable — gives its
products no printed parts, and the plan therefore nothing to count. This module
is the other half: it re-parses exactly those files with the SAME extractor the
upload uses, writes the objects back, and puts every file that belongs to a
product through :func:`~backend.app.services.product_sync.sync_product_for_file`
— the single writer of the pivot, the plates and the seeded parts. Objects
without that sync change nothing anybody can see.

Two callers, one function (spec §G, ruling of 2026-09-04 — **no manual script**,
because a user who cannot run one is exactly the user this exists for):

1. ``m158.seed()`` at upgrade, between the archive backfill and rule D;
2. ``main.py``'s lifespan, once per boot, as a cancellable background task — so
   a mount that was down during the upgrade is picked up at a later start.

**Idempotent by construction: the worklist query is the marker.** Nothing is
stamped anywhere; a file that has objects is not selected, so a ``DEBUG=true``
re-run of the seed and every boot after it cost one SELECT.

⚠️ **The m148 rule holds here too** — the stat and the unzip happen in a thread
with no session open, and the writes go in ``batch_size`` chunks, each its own
short transaction. Opening a session around a walk of a network share is what
made unrelated queries die with ``database is locked``.

⚠️ **Unreachable is not empty.** A file whose mount is down still describes real
objects; writing "no objects" for it would be a data loss dressed up as a
backfill. It is counted, skipped, and tried again at the next start.

⚠️ **This writes objects; it does not re-derive the row's metadata.** Everything
else the row holds — print time, filaments, tags, the plate names — was written
by the parse that stored it and is left exactly as found.
"""

from __future__ import annotations

import asyncio
import logging
import zipfile
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select, update

from backend.app.models.library import LibraryFile
from backend.app.models.product import product_files
from backend.app.services.library_helpers import SLICED_GCODE_META_KEY
from backend.app.services.product_sync import sync_product_for_file

logger = logging.getLogger(__name__)

#: Chunk size for the write half. Sized for LOCK TIME, not commit cost — WAL
#: plus ``synchronous=NORMAL`` means a commit costs no fsync (the m148 rule).
BATCH_SIZE = 25

_UNREACHABLE = "unreachable"
_UNPARSEABLE = "unparseable"
_OK = "ok"


@dataclass
class BackfillSummary:
    """What one sweep did.

    ``products_synced`` counts DISTINCT products reconciled, not sync calls: one
    file can belong to several, and several files to one.
    """

    scanned: int = 0
    filled: int = 0
    skipped_unreachable: int = 0
    skipped_unparseable: int = 0
    products_synced: int = 0


def _is_3mf_container(filename: str | None) -> bool:
    """``.3mf`` and ``.gcode.3mf`` alike — the suffix is the container, and a
    sliced file is the one that matters most here."""
    return bool(filename) and filename.lower().endswith(".3mf")


def _has_objects(meta: dict | None) -> bool:
    """True when this row already knows its objects, at the top level or on any
    plate. Both are checked because a multi-plate file's top level never carries
    the other plates — reading only one of the two would put half the library
    back on the worklist at every boot."""
    if not isinstance(meta, dict):
        return False
    top = meta.get("printable_objects")
    if isinstance(top, dict) and top:
        return True
    for plate in meta.get("plates") or []:
        objects = plate.get("printable_objects") if isinstance(plate, dict) else None
        if isinstance(objects, dict) and objects:
            return True
    return False


def _cannot_have_objects(meta: dict | None) -> bool:
    """``has_sliced_gcode is False`` is the parse's own answer: this container
    holds no ``Metadata/*.gcode``, so it has no printable objects and never
    will. Excluded from the worklist because it is the one class of file that
    would otherwise be re-opened on every boot, forever, to learn the same
    nothing. ``None`` is not ``False`` — an unreadable file has not been shown
    to lack g-code (see ``library_helpers.sliced_gcode_in_3mf``).
    """
    return isinstance(meta, dict) and meta.get(SLICED_GCODE_META_KEY) is False


async def files_missing_objects(session_factory) -> list[tuple[int, str]]:
    """``(id, file_path)`` of every active 3MF container that has no objects.

    ⚠️ **Named columns only.** This runs inside ``m158.seed()``, mid-chain: an
    entity-wide ``select(LibraryFile)`` would emit whatever the model has TODAY
    and break the upgrade the day a later migration adds a column.

    The JSON test is done in Python rather than in SQL on purpose — the shape
    differs between SQLite and PostgreSQL, and this list is short enough that a
    full scan of the id/metadata columns is cheaper than two dialect-specific
    queries to maintain.
    """
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(
                    LibraryFile.id,
                    LibraryFile.filename,
                    LibraryFile.file_path,
                    LibraryFile.file_metadata,
                )
                .where(LibraryFile.deleted_at.is_(None))
                .order_by(LibraryFile.id)
            )
        ).all()
    return [
        (file_id, file_path)
        for file_id, filename, file_path, meta in rows
        if _is_3mf_container(filename) and file_path and not _has_objects(meta) and not _cannot_have_objects(meta)
    ]


def _extract_objects(file_path: str) -> tuple[str, dict, list[dict]]:
    """``(status, printable_objects, plates)`` for one file — blocking, threaded.

    The extractor is the library UPLOAD's, verbatim: ``ThreeMFParser`` for the
    top-level objects of the default plate and ``parse_plates_from_3mf`` for the
    per-plate ones, so a backfilled row is indistinguishable from a fresh one.

    ⚠️ ``ThreeMFParser.parse`` swallows its own failures and returns partial
    metadata, so it can never tell us a file is corrupt. The ZIP open does, and
    it comes first for that reason.
    """
    from backend.app.api.routes.library import to_absolute_path
    from backend.app.services.archive import ThreeMFParser, parse_plates_from_3mf

    try:
        path = to_absolute_path(file_path)
        if path is None or not Path(path).is_file():
            return _UNREACHABLE, {}, []
    except OSError:
        # A hung mount answers a stat with an error, not with an empty file.
        return _UNREACHABLE, {}, []

    try:
        with zipfile.ZipFile(str(path), "r") as zf:
            plates = parse_plates_from_3mf(zf)
        objects = ThreeMFParser(str(path)).parse().get("printable_objects")
    except Exception:  # noqa: BLE001 — one bad 3MF may never sink the sweep
        logger.debug("library object backfill: unparseable 3MF %s", file_path, exc_info=True)
        return _UNPARSEABLE, {}, []

    return _OK, objects if isinstance(objects, dict) else {}, plates


def _merged_metadata(meta: dict | None, objects: dict, plates: list[dict]) -> dict | None:
    """The row's metadata with the objects laid in — ``None`` when nothing to add.

    ``None`` matters: it is what keeps a file whose 3MF genuinely holds no
    objects (a source-only container) from being rewritten, and ``updated_at``
    re-stamped, on every pass.
    """
    out = dict(meta or {})
    changed = False

    if objects:
        out["printable_objects"] = objects
        changed = True

    if plates:
        existing = out.get("plates")
        if isinstance(existing, list) and existing:
            fresh_by_index = {p["index"]: p for p in plates if isinstance(p.get("index"), int)}
            merged: list = []
            for plate in existing:
                fresh = fresh_by_index.get(plate.get("index")) if isinstance(plate, dict) else None
                fresh_objects = (fresh or {}).get("printable_objects")
                if isinstance(fresh_objects, dict) and fresh_objects and not plate.get("printable_objects"):
                    # ``object_count`` moves with them: it counts INSTANCES, and
                    # the ``objects`` list it fell back to is name-deduplicated,
                    # so ten clones read as one until the objects arrive.
                    plate = {**plate, "printable_objects": fresh_objects, "object_count": len(fresh_objects)}
                    changed = True
                merged.append(plate)
            out["plates"] = merged
        else:
            # No cached plate list at all (a row from before m023, or one whose
            # scan could not open the file). The upload writes both keys
            # together; so do we, or a multi-plate file keeps a single plate 0.
            out["plates"] = plates
            out["is_multi_plate"] = len(plates) > 1
            changed = True

    return out if changed else None


async def _write_chunk(session_factory, chunk: list[tuple[int, dict, list[dict]]]) -> tuple[int, set[int]]:
    """One short transaction: write the objects, then reconcile the products.

    Returns ``(files written, product ids reconciled)``. The sync rides inside
    the same transaction — it reads ``file_metadata`` back, and it must read the
    value this chunk just wrote.
    """
    written = 0
    synced: set[int] = set()
    async with session_factory() as session:
        ids = [file_id for file_id, _, _ in chunk]
        current = dict(
            (
                await session.execute(select(LibraryFile.id, LibraryFile.file_metadata).where(LibraryFile.id.in_(ids)))
            ).all()
        )
        filled_ids: list[int] = []
        for file_id, objects, plates in chunk:
            if file_id not in current:
                # Deleted between the worklist and here. Not our business.
                continue
            merged = _merged_metadata(current[file_id], objects, plates)
            if merged is None:
                continue
            await session.execute(update(LibraryFile).where(LibraryFile.id == file_id).values(file_metadata=merged))
            filled_ids.append(file_id)
            written += 1

        for file_id in filled_ids:
            # ⚠️ Read off the pivot, never off ``LibraryFile.products``: a lazy
            # collection in an async session is a ``MissingGreenlet``, and the
            # pivot is what ``sync_product_for_file`` reconciles against anyway.
            product_ids = sorted(
                (
                    await session.execute(
                        select(product_files.c.product_id).where(product_files.c.library_file_id == file_id)
                    )
                )
                .scalars()
                .all()
            )
            if not product_ids:
                continue
            try:
                await sync_product_for_file(session, library_file_id=file_id, product_ids=product_ids)
            except Exception:  # noqa: BLE001 — one product may not sink the sweep
                logger.warning("library object backfill: product resync failed for file %s", file_id, exc_info=True)
                continue
            synced.update(product_ids)

        await session.commit()
    return written, synced


async def backfill_library_objects(session_factory, *, batch_size: int = BATCH_SIZE) -> BackfillSummary:
    """Fill in the object metadata of every library 3MF that has none.

    Never raises: an unreachable path is counted and retried at the next start,
    an unparseable one is counted and logged. ``asyncio.CancelledError`` is NOT
    swallowed here — shutdown must be able to stop this mid-file, and the caller
    in the lifespan is where it is absorbed.
    """
    summary = BackfillSummary()
    worklist = await files_missing_objects(session_factory)
    summary.scanned = len(worklist)
    if not worklist:
        return summary

    synced: set[int] = set()
    for start in range(0, len(worklist), batch_size):
        chunk: list[tuple[int, dict, list[dict]]] = []
        for file_id, file_path in worklist[start : start + batch_size]:
            status, objects, plates = await asyncio.to_thread(_extract_objects, file_path)
            if status == _UNREACHABLE:
                summary.skipped_unreachable += 1
                continue
            if status == _UNPARSEABLE:
                summary.skipped_unparseable += 1
                continue
            chunk.append((file_id, objects, plates))
        if not chunk:
            continue
        try:
            written, chunk_synced = await _write_chunk(session_factory, chunk)
        except Exception:  # noqa: BLE001 — a failed chunk may not sink the rest
            logger.warning("library object backfill: chunk of %d file(s) failed", len(chunk), exc_info=True)
            continue
        summary.filled += written
        synced.update(chunk_synced)

    summary.products_synced = len(synced)
    return summary


__all__ = ["BackfillSummary", "backfill_library_objects", "files_missing_objects"]
