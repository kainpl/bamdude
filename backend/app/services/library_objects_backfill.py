"""Library 3MFs that carry no object metadata get it — at upgrade and at boot.

``print_archive_parts`` is backfilled from ARCHIVE 3MFs by m158. A LIBRARY 3MF
whose ``file_metadata`` never got ``printable_objects`` — uploaded before the
extractor existed, or scanned while its mount was unreachable — gives its
products no printed parts, and the plan therefore nothing to count. This module
is the other half: it re-parses exactly those files with the SAME extractor the
upload uses, writes the objects back, and (outside a migration) puts every file
that belongs to a product through
:func:`~backend.app.services.product_sync.sync_product_for_file` — the single
writer of the pivot, the plates and the seeded parts. Objects without that sync
change nothing anybody can see.

Two callers, one function (spec §G, ruling of 2026-09-04 — **no manual script**,
because a user who cannot run one is exactly the user this exists for):

1. ``m158.seed()`` at upgrade, FIRST, with ``sync_products=False`` — a migration
   mid-chain may not emit an entity-wide ORM select, and ``sync_product_for_file``
   emits two. m158 seeds the parts itself, in text SQL, from the metadata this
   pass has just filled;
2. ``main.py``'s lifespan, once per boot, as a cancellable background task with
   the sync ON — so a mount that was down during the upgrade is picked up at a
   later start, products and all.

**Idempotent by construction: the worklist query is the marker.** Nothing is
stamped anywhere; a file whose metadata records the objects is not selected, so
a ``DEBUG=true`` re-run of the seed and every boot after it cost one SELECT.

⚠️ **"Recorded" means the KEY, not a non-empty value.** ``parse_plates_from_3mf``
writes ``printable_objects`` on every plate it finds, ``{}`` included — a plate
that genuinely holds nothing has been ASKED, and asking again every boot is what
the marker exists to prevent. Keying on emptiness instead re-opened those files
forever.

⚠️ **Never key on ``has_sliced_gcode``.** It says only whether the container
packs ``Metadata/*.gcode``, and objects have a third source —
``Metadata/slice_info.config`` (``archive.py::discover_plate_objects`` tier 3),
which Bambu Studio writes on every save of a sliced plate while g-code is packed
only on "export sliced file". m137 stamped ``has_sliced_gcode=False`` across
every legacy project ``.3mf``, so excluding on it skips exactly the files this
module exists for.

⚠️ **The m148 rule holds here too** — the stat and the unzip happen in a thread
with no session open, and the writes go in ``batch_size`` chunks, each its own
short transaction. Opening a session around a walk of a network share is what
made unrelated queries die with ``database is locked``.

⚠️ **Unreachable is not empty.** A file whose mount is down still describes real
objects; writing "no objects" for it would be a data loss dressed up as a
backfill. It is counted, skipped, and tried again at the next start.

⚠️ **This writes objects; it does not re-derive the row's metadata.** The keys it
lays in are the ones the upload writes that bear on objects —
``printable_objects``, ``plates``, ``is_multi_plate``, and (only alongside them,
only when the row has none) the two ``skip_objects_supported`` inputs. Print
time, filaments, tags and plate names are left exactly as found.
"""

from __future__ import annotations

import asyncio
import logging
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import func, select, update

from backend.app.models.library import LibraryFile
from backend.app.services.library_helpers import skip_objects_supported_from_metadata
from backend.app.services.product_sync import _linked_product_ids, sync_product_for_file

logger = logging.getLogger(__name__)

#: Chunk size for the write half. Sized for LOCK TIME, not commit cost — WAL
#: plus ``synchronous=NORMAL`` means a commit costs no fsync (the m148 rule).
BATCH_SIZE = 25

#: The two ``project_settings.config`` flags ``skip_objects_supported`` is
#: derived from. Carried so a backfilled row derives the column from the same
#: inputs a fresh upload does.
_SKIP_FLAGS = ("gcode_label_objects", "exclude_object")

_UNREACHABLE = "unreachable"
_UNPARSEABLE = "unparseable"
_OK = "ok"


@dataclass
class BackfillSummary:
    """What one sweep did.

    ``products_synced`` counts DISTINCT products reconciled, not sync calls: one
    file can belong to several, and several files to one. It is always 0 when
    the caller passed ``sync_products=False``.

    ``filled_ids`` names the rows THIS run wrote — the scope a caller that has
    to do its own follow-up work must confine itself to. ``m158.seed()`` derives
    printed parts from it: walking every product plate instead would re-create an
    ``auto`` part an operator deleted on a product this run never touched.
    """

    scanned: int = 0
    filled: int = 0
    skipped_unreachable: int = 0
    skipped_unparseable: int = 0
    products_synced: int = 0
    filled_ids: list[int] = field(default_factory=list)


def _objects_recorded(meta: dict | None) -> bool:
    """True when this row has already been ASKED what its objects are.

    The test is the presence of the ``printable_objects`` key — at the top level
    or on any cached plate — never whether it holds anything. An empty map is an
    answer (``parse_per_plate_skip_metadata`` writes one for a plate it could
    find nothing on); treating it as "unknown" puts the file back on the
    worklist at every boot for ever. Both levels are checked because a
    multi-plate file's top level never carries the other plates.
    """
    if not isinstance(meta, dict):
        return False
    if "printable_objects" in meta:
        return True
    return any(isinstance(plate, dict) and "printable_objects" in plate for plate in meta.get("plates") or [])


async def files_missing_objects(session_factory) -> list[tuple[int, str]]:
    """``(id, file_path)`` of every active 3MF container that has no objects.

    ⚠️ **Named columns only.** This runs inside ``m158.seed()``, mid-chain: an
    entity-wide ``select(LibraryFile)`` would emit whatever the model has TODAY
    and break the upgrade the day a later migration adds a column.

    ⚠️ **The filename filter is done in SQL** (``lower(filename) LIKE '%.3mf'``,
    which covers ``.gcode.3mf``), so a library of STLs and raw g-code never
    ships its ``file_metadata`` blobs to Python to be JSON-parsed on the event
    loop. The JSON test stays in Python: the shape differs between SQLite and
    PostgreSQL, and two dialect-specific queries would be two things to keep in
    step for no gain over a list this short.
    """
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(LibraryFile.id, LibraryFile.file_path, LibraryFile.file_metadata)
                .where(
                    LibraryFile.deleted_at.is_(None),
                    func.lower(LibraryFile.filename).like("%.3mf"),
                )
                .order_by(LibraryFile.id)
            )
        ).all()
    return [(file_id, file_path) for file_id, file_path, meta in rows if file_path and not _objects_recorded(meta)]


def _extract_objects(file_path: str) -> tuple[str, dict]:
    """``(status, parsed)`` for one file — blocking, meant for a worker thread.

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
            return _UNREACHABLE, {}
    except OSError:
        # A hung mount answers a stat with an error, not with an empty file.
        return _UNREACHABLE, {}

    try:
        with zipfile.ZipFile(str(path), "r") as zf:
            plates = parse_plates_from_3mf(zf)
        meta = ThreeMFParser(str(path)).parse()
    except Exception:  # noqa: BLE001 — one bad 3MF may never sink the sweep
        logger.debug("library object backfill: unparseable 3MF %s", file_path, exc_info=True)
        return _UNPARSEABLE, {}

    objects = meta.get("printable_objects")
    parsed: dict = {
        "printable_objects": objects if isinstance(objects, dict) else {},
        "plates": plates,
    }
    for flag in _SKIP_FLAGS:
        if flag in meta:
            parsed[flag] = meta[flag]
    return _OK, parsed


def _merged_metadata(meta: dict | None, parsed: dict) -> dict | None:
    """The row's metadata with the objects laid in — ``None`` when nothing to add.

    ``None`` matters: it is what keeps a file whose 3MF genuinely holds no
    objects from being rewritten, and ``updated_at`` re-stamped, on every pass.
    """
    out = dict(meta or {})
    changed = False

    objects = parsed.get("printable_objects") or {}
    if objects:
        out["printable_objects"] = objects
        changed = True

    plates = parsed.get("plates") or []
    if plates:
        fresh = {p["index"]: p for p in plates if isinstance(p, dict) and isinstance(p.get("index"), int)}
        existing = out.get("plates")
        if isinstance(existing, list) and existing:
            merged: list = []
            seen: set[int] = set()
            for plate in existing:
                if isinstance(plate, dict) and isinstance(plate.get("index"), int):
                    seen.add(plate["index"])
                    fresh_objects = (fresh.get(plate["index"]) or {}).get("printable_objects")
                    if isinstance(fresh_objects, dict) and fresh_objects and not plate.get("printable_objects"):
                        # ``object_count`` moves with them: it counts INSTANCES,
                        # and the ``objects`` list it fell back to is
                        # name-deduplicated, so ten clones read as one until the
                        # objects arrive.
                        plate = {**plate, "printable_objects": fresh_objects, "object_count": len(fresh_objects)}
                        changed = True
                merged.append(plate)
            # A cached list can be SHORTER than the file — the parse that wrote
            # it saw fewer plates, or was cut off. Those plates exist and their
            # objects are printable; leaving them out keeps the product blind to
            # them for ever.
            appended = [fresh[index] for index in sorted(fresh) if index not in seen]
            if appended:
                merged.extend(appended)
                changed = True
            out["plates"] = merged
            out["is_multi_plate"] = len(merged) > 1
        else:
            # No cached plate list at all (a row from before the plates cache, or
            # one whose scan could not open the file). The upload writes both
            # keys together; so do we, or a multi-plate file keeps a single
            # plate 0.
            out["plates"] = plates
            out["is_multi_plate"] = len(plates) > 1
            changed = True

    if changed:
        # ⚠️ Only alongside a real fill, and never over a value the row already
        # has. On their own these two would rewrite a row the sweep cannot
        # otherwise help — and, carrying no ``printable_objects`` key, it would
        # be rewritten again at every boot.
        for flag in _SKIP_FLAGS:
            if flag in parsed and flag not in out:
                out[flag] = parsed[flag]

    return out if changed else None


async def _write_chunk(
    session_factory, chunk: list[tuple[int, dict]], *, sync_products: bool
) -> tuple[list[int], set[int]]:
    """One short transaction: write the objects, then reconcile the products.

    Returns ``(file ids written, product ids reconciled)``. The sync rides inside
    the same transaction — it reads ``file_metadata`` back, and it must read the
    value this chunk just wrote.
    """
    synced: set[int] = set()
    async with session_factory() as session:
        ids = [file_id for file_id, _ in chunk]
        current = dict(
            (
                await session.execute(select(LibraryFile.id, LibraryFile.file_metadata).where(LibraryFile.id.in_(ids)))
            ).all()
        )
        filled_ids: list[int] = []
        for file_id, parsed in chunk:
            if file_id not in current:
                # Deleted between the worklist and here. Not our business.
                continue
            merged = _merged_metadata(current[file_id], parsed)
            if merged is None:
                continue
            await session.execute(
                update(LibraryFile)
                .where(LibraryFile.id == file_id)
                .values(
                    file_metadata=merged,
                    # Derived exactly as ``save_3mf_bytes_to_library`` derives it
                    # at construction, from the metadata being written — a
                    # backfilled row must not badge differently from a fresh one.
                    skip_objects_supported=skip_objects_supported_from_metadata(merged),
                )
            )
            filled_ids.append(file_id)

        if sync_products:
            for file_id in filled_ids:
                # ⚠️ Off the pivot, never off ``LibraryFile.products``: a lazy
                # collection in an async session is a ``MissingGreenlet``. Same
                # reader ``resync_file_products`` uses, so "who is linked" has
                # one answer.
                product_ids = sorted(await _linked_product_ids(session, file_id))
                if not product_ids:
                    continue
                try:
                    await sync_product_for_file(session, library_file_id=file_id, product_ids=product_ids)
                except Exception:  # noqa: BLE001 — one product may not sink the sweep
                    logger.warning("library object backfill: product resync failed for file %s", file_id, exc_info=True)
                    continue
                synced.update(product_ids)

        await session.commit()
    return filled_ids, synced


async def backfill_library_objects(
    session_factory, *, batch_size: int = BATCH_SIZE, sync_products: bool = True
) -> BackfillSummary:
    """Fill in the object metadata of every library 3MF that has none.

    Never raises. An unreachable path is counted and retried at the next start,
    an unparseable one is counted and logged, a chunk that will not write is
    logged and the rest still land, and even the worklist query itself is
    guarded — it runs inside a migration's ``seed()``, where an exception would
    leave the upgrade unrecorded and re-entered on the next boot.

    ``sync_products=False`` writes the metadata and touches no product. It
    exists for ``m158.seed()``: ``sync_product_for_file`` emits entity-wide ORM
    selects against TODAY's models, which a migration running mid-chain may not
    do. The migration seeds the parts itself, in text SQL.

    ``asyncio.CancelledError`` is deliberately NOT swallowed — shutdown must be
    able to stop this mid-file, and the lifespan wrapper is where it is absorbed.
    """
    summary = BackfillSummary()
    try:
        worklist = await files_missing_objects(session_factory)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — a failed worklist may not abort a migration
        logger.warning("library object backfill: the worklist query failed", exc_info=True)
        return summary
    summary.scanned = len(worklist)
    if not worklist:
        return summary

    synced: set[int] = set()
    for start in range(0, len(worklist), batch_size):
        chunk: list[tuple[int, dict]] = []
        for file_id, file_path in worklist[start : start + batch_size]:
            status, parsed = await asyncio.to_thread(_extract_objects, file_path)
            if status == _UNREACHABLE:
                summary.skipped_unreachable += 1
                continue
            if status == _UNPARSEABLE:
                summary.skipped_unparseable += 1
                continue
            chunk.append((file_id, parsed))
        if not chunk:
            continue
        try:
            written_ids, chunk_synced = await _write_chunk(session_factory, chunk, sync_products=sync_products)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a failed chunk may not sink the rest
            logger.warning("library object backfill: chunk of %d file(s) failed", len(chunk), exc_info=True)
            continue
        summary.filled += len(written_ids)
        summary.filled_ids.extend(written_ids)
        synced.update(chunk_synced)

    summary.products_synced = len(synced)
    return summary


__all__ = ["BackfillSummary", "backfill_library_objects", "files_missing_objects"]
