"""Add ``print_archives.plate_index`` + per-plate metadata backfill.

Multi-plate prints used to lose the "which plate was printed" signal
the moment the queue row was hard-deleted post-completion. The only
trace was an optional " - Plate N" suffix the parser stuck on
``print_name`` for plate ≥ 2 — human-readable, not queryable, and
silent for plate 1. Worse: the archive's thumbnail and slicer-derived
metadata (print_time, weight, layer count, printable objects, per-slot
filament usage) all came from plate 1 of the container regardless of
what actually ran, because ``ThreeMFParser._parse_slice_info`` always
took the first ``<plate>`` element it found.

m038 introduces an explicit ``plate_index`` column on ``print_archives``
so:

- the file manager can show the right per-plate thumbnail under
  "what was printed";
- the G-code viewer can fetch the actual printed plate's gcode (not
  just the first ``Metadata/plate_*.gcode`` entry the lib happens to
  list);
- the 3D model viewer can pre-select that plate in the source 3MF;
- queries like "how often did plate 2 of project X run?" become
  trivial.

The migration runs in two phases:

**Phase A — backfill ``plate_index``** (per row, in priority order;
first signal that yields a value wins):

1. **Open the archive 3MF**, read ``Metadata/slice_info.config``, find
   the ``<plate>`` element's ``index`` metadata. Single-plate exports
   (the typical Bambu Studio "Send to printer" output) carry exactly
   one ``<plate>`` whose index says which plate of the original
   project was exported. This is the canonical signal and matches
   the write-side at runtime.
2. **Parse the print_name suffix** — ``ThreeMFParser`` historically
   appended " - Plate N" (N ≥ 2) to ``print_name``. If the on-disk
   3MF is missing or unparseable but the suffix is present we can
   still recover N. Plate 1 had no suffix so this branch can't tell
   "plate 1" from "single-plate".
3. **Leave NULL** when both fail — likely an external print whose
   plate origin is genuinely unknown. The runtime write-side covers
   future inserts.

**Phase B — re-parse multi-plate archives** so historical rows pick
up the right plate's slicer-derived metadata + thumbnail. Only runs
on rows where ``plate_index > 1`` AND the source 3MF is still on
disk (plate 1 archives are already correct, plate-1-fallback parser
behaviour matches the new plate-aware logic for those rows). For
each row:

- re-extract the thumbnail at ``Metadata/plate_{N}.png`` and
  overwrite the existing thumbnail file in place;
- update ``print_time_seconds``, ``filament_used_grams``,
  ``filament_type``, ``filament_color``, ``total_layers`` from the
  re-parsed metadata;
- merge ``printable_objects``, ``filament_slots``, and the
  ``plate_id`` mirror into ``extra_data`` without disturbing
  user-edited or system keys (notes, tags, swap_macro_events_pending,
  no_3mf_available, etc. all stay).

Cost: opens every archive 3MF on disk twice (once per phase). Same
shape as m022/m023 backfills; one-time on upgrade. In dev
(``DEBUG=true``) re-runs each restart, which is fine because both
phases are idempotent — phase A skips rows with ``plate_index NOT
NULL``, phase B re-parses and writes the same values.
"""

from __future__ import annotations

import logging
import re
import zipfile
from pathlib import Path

import defusedxml.ElementTree as ET
from sqlalchemy import select, update

from backend.app.core.config import settings
from backend.app.migrations.helpers import add_column
from backend.app.models.archive import PrintArchive

logger = logging.getLogger(__name__)

version = 38
name = "archive_plate_index"

# Matches " - Plate 7" / " — Plate 12" suffixes the parser appended.
# Only fires for N ≥ 2 historically, so plate 1 archives never carry it.
_NAME_SUFFIX_RE = re.compile(r"\s[-—]\s*Plate\s+(\d+)\s*$", re.IGNORECASE)


async def upgrade(conn):
    added = await add_column(conn, "print_archives", "plate_index INTEGER")
    if not added:
        # Already there — m022/m023 set the precedent of bailing early
        # when the schema change is a no-op so the (potentially long)
        # backfill doesn't redundantly hammer the disk.
        return


def _extract_plate_index_from_3mf(archive_path: Path) -> int | None:
    """Read ``Metadata/slice_info.config`` and return the plate index.

    Mirrors the write-side logic in ``ThreeMFParser._parse_slice_info``
    — single-plate exports always have one ``<plate>`` element with an
    ``index`` metadata key. Multi-plate containers (rare in archive
    context — Bambu Studio normally exports per-plate when sending to
    the printer) have several; we pick the first plate's index, but
    this is ambiguous and the caller should fall through to the name
    suffix when in doubt.

    Returns ``None`` on missing file, missing config, malformed XML,
    or absent index metadata.
    """
    try:
        if not archive_path.is_file():
            return None
        with zipfile.ZipFile(archive_path, "r") as zf:
            if "Metadata/slice_info.config" not in zf.namelist():
                return None
            root = ET.fromstring(zf.read("Metadata/slice_info.config").decode("utf-8"))
            plates = root.findall(".//plate")
            if not plates:
                return None
            # Single-plate export: trust this as the canonical signal.
            # Multi-plate: the first plate's index is the lowest plate
            # in the container, which doesn't tell us which one ran.
            # Return None so the caller can fall through to the suffix
            # heuristic instead of guessing.
            if len(plates) > 1:
                return None
            for meta in plates[0].findall("metadata"):
                if meta.get("key") == "index":
                    try:
                        return int(meta.get("value", ""))
                    except (ValueError, TypeError):
                        return None
    except (zipfile.BadZipFile, ET.ParseError, OSError):
        return None
    return None


def _extract_plate_index_from_name(print_name: str | None) -> int | None:
    """Recover plate index from a " - Plate N" suffix on ``print_name``.

    The historical write-side appended this for plate ≥ 2 only, so the
    absence of a suffix is consistent with "plate 1" but not proof of
    it — the caller must not infer plate 1 from a None return here.
    """
    if not print_name:
        return None
    match = _NAME_SUFFIX_RE.search(print_name)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (ValueError, TypeError):
        return None


async def seed(session_factory):
    await _backfill_plate_index(session_factory)
    await _reparse_multi_plate_archives(session_factory)


async def _backfill_plate_index(session_factory):
    """Phase A — fill ``plate_index`` for archives where we can recover it.

    Streams in batches of 200. The 3MF read is the slow step (zip
    open + XML parse), so a smaller batch than the m036 tag backfill
    keeps memory steady without ballooning the WAL. Per-row guard
    ``plate_index IS NULL`` makes a re-run idempotent.
    """
    async with session_factory() as db:
        offset = 0
        batch_size = 200
        recovered = 0
        examined = 0

        while True:
            rows = (
                await db.execute(
                    select(
                        PrintArchive.id,
                        PrintArchive.file_path,
                        PrintArchive.print_name,
                    )
                    .where(PrintArchive.plate_index.is_(None))
                    .order_by(PrintArchive.id)
                    .offset(offset)
                    .limit(batch_size)
                )
            ).all()
            if not rows:
                break

            for row in rows:
                examined += 1
                resolved: int | None = None

                # 1. Open the archive 3MF and read slice_info.config.
                if row.file_path:
                    try:
                        abs_path = settings.base_dir / row.file_path
                        resolved = _extract_plate_index_from_3mf(abs_path)
                    except Exception as exc:  # noqa: BLE001 — defensive, don't break the loop
                        logger.debug("m038: 3MF probe failed for archive %s: %s", row.id, exc)

                # 2. Fall back to the print_name " - Plate N" suffix.
                if resolved is None:
                    resolved = _extract_plate_index_from_name(row.print_name)

                if resolved is not None and resolved > 0:
                    await db.execute(update(PrintArchive).where(PrintArchive.id == row.id).values(plate_index=resolved))
                    recovered += 1

            await db.commit()
            offset += batch_size
            if len(rows) < batch_size:
                break

        if examined:
            logger.info(
                "m038[A]: examined %s archives, recovered plate_index for %s (%.0f%%)",
                examined,
                recovered,
                100 * recovered / examined,
            )


async def _reparse_multi_plate_archives(session_factory):
    """Phase B — re-parse archives where ``plate_index > 1`` so the
    thumbnail + slicer-derived metadata reflect the actually-printed
    plate (instead of plate 1 of the container, which is what the
    pre-m038 parser always extracted).

    Plate 1 archives are skipped: the old "find the first <plate>
    element" parser behaviour incidentally matched the right plate
    for them, so re-parsing wouldn't change anything and we'd just
    waste IO on every existing single-plate row.

    Idempotent — the parser is a pure function of the 3MF + plate
    index, so re-runs write the same values.
    """
    # Imports are local to keep the module-import cost low for the
    # common case (migration table check + fast skip on already-applied).
    from backend.app.services.archive import ThreeMFParser

    async with session_factory() as db:
        rows = (
            (
                await db.execute(
                    select(PrintArchive)
                    .where(PrintArchive.plate_index.isnot(None), PrintArchive.plate_index > 1)
                    .order_by(PrintArchive.id)
                )
            )
            .scalars()
            .all()
        )

        examined = 0
        updated = 0
        thumbnails_written = 0
        for archive in rows:
            examined += 1
            if not archive.file_path:
                # Fallback / no_3mf_available archive — nothing to re-parse.
                continue
            archive_path = settings.base_dir / archive.file_path
            if not archive_path.is_file() or not zipfile.is_zipfile(archive_path):
                continue

            try:
                parser = ThreeMFParser(archive_path, plate_number=archive.plate_index)
                metadata = parser.parse()
            except Exception as exc:  # noqa: BLE001 — keep the loop going
                logger.debug("m038[B]: re-parse failed for archive %s: %s", archive.id, exc)
                continue

            # Slicer-derived columns. Only overwrite when the new value
            # is non-None — the parser may legitimately return None for
            # a field if the slice_info entry doesn't carry it, and we
            # don't want to clobber a valid old value with a fresh None.
            new_print_time = metadata.get("print_time_seconds")
            new_filament_grams = metadata.get("filament_used_grams")
            new_filament_type = metadata.get("filament_type")
            new_filament_color = metadata.get("filament_color")
            new_total_layers = metadata.get("total_layers")

            changed = False
            if new_print_time is not None and archive.print_time_seconds != new_print_time:
                archive.print_time_seconds = new_print_time
                changed = True
            if new_filament_grams is not None and archive.filament_used_grams != new_filament_grams:
                archive.filament_used_grams = new_filament_grams
                changed = True
            if new_filament_type and archive.filament_type != new_filament_type:
                archive.filament_type = new_filament_type
                changed = True
            if new_filament_color and archive.filament_color != new_filament_color:
                archive.filament_color = new_filament_color
                changed = True
            if new_total_layers is not None and archive.total_layers != new_total_layers:
                archive.total_layers = new_total_layers
                changed = True

            # Merge per-plate keys into existing extra_data without
            # touching user-edited / system fields. Keys we own:
            # ``printable_objects``, ``filament_slots``, ``plate_id``
            # (mirror of the column for legacy queue_virtual reader).
            existing_extra = dict(archive.extra_data or {})
            for key in ("printable_objects", "filament_slots"):
                if key in metadata and existing_extra.get(key) != metadata[key]:
                    existing_extra[key] = metadata[key]
                    changed = True
            if existing_extra.get("plate_id") != archive.plate_index:
                existing_extra["plate_id"] = archive.plate_index
                changed = True
            if changed:
                archive.extra_data = existing_extra

            # Thumbnail: parser surfaces ``_thumbnail_data`` /
            # ``_thumbnail_ext`` only when the targeted plate's PNG
            # was found in the zip. Overwrite the existing thumbnail
            # file in place — same path, new bytes — so any cached
            # image URL keeps working.
            thumb_data = metadata.get("_thumbnail_data")
            if thumb_data and archive.thumbnail_path:
                try:
                    thumb_abs = settings.base_dir / archive.thumbnail_path
                    if thumb_abs.parent.is_dir():
                        thumb_abs.write_bytes(thumb_data)
                        thumbnails_written += 1
                except OSError as exc:
                    logger.debug(
                        "m038[B]: thumbnail rewrite failed for archive %s: %s",
                        archive.id,
                        exc,
                    )

            if changed:
                updated += 1

        if examined:
            await db.commit()
            logger.info(
                "m038[B]: examined %s multi-plate archives, updated metadata on %s, rewrote %s thumbnails",
                examined,
                updated,
                thumbnails_written,
            )
