"""Add ``skip_objects_supported`` and refresh the object caches.

Three facts land per row, from a single ZIP open each:

* ``skip_objects_supported`` (new column) — ``gcode_label_objects AND
  exclude_object`` from ``Metadata/project_settings.config``. m022 already put
  both flags into the JSON columns, so rows whose file is gone can still be
  answered from what is stored.

* ``printable_objects`` (existing JSON key) — re-extracted with the
  evidence-ranked cascade (gcode ``; model label id:`` → pick PNG → slice_info).
  Rows written before that cascade existed took slice_info alone, which
  undercounts every OrcaSlicer plate that uses instances: the control file
  listed 1 object where the printer printed 5. ``object_count`` is derived from
  this dict and now gates a UI affordance, so a stale count hides a working
  button.

* ``plates`` (existing JSON key, filled by m023) — rebuilt from the same open
  ZIP. Its per-plate object lists had the identical defect, fixed one commit
  earlier in ``parse_per_plate_skip_metadata``. Refreshing the file total while
  leaving the per-plate cache stale would make a file disagree with the sum of
  its own plates, which reads as a bug in both numbers rather than one.

Importing from ``services.archive`` here is deliberate. m023 imports
``parse_plates_from_3mf`` and m036 imports ``compute_file_tags`` for the same
reason: this backfills a *derived cache*, where a later version's better
extractor producing a better result is the goal, not drift. m022's
copy-the-helper rule guards frozen semantics, which this is not.

Never erases: each JSON key is overwritten only when its extractor returned
something non-empty, so an unreadable plate cannot destroy a value that was
already right.

**Long-startup warning**: opens every 3MF still on disk, ~50-200 ms each, and
the per-plate pass decodes one pick PNG per plate. On an install with thousands
of archives this is minutes before the API answers. Progress is logged every 100
rows.
"""

from __future__ import annotations

import json
import logging
import zipfile
from pathlib import Path

from sqlalchemy import text

from backend.app.core.config import settings
from backend.app.migrations.helpers import add_column

logger = logging.getLogger(__name__)

version = 114
name = "skip_objects_supported"


# Commit batch size — keeps the SQLite write-lock window short so a parallel
# reader doesn't time out. Same value and rationale as m022.
_BATCH_SIZE = 100


async def upgrade(conn):
    await add_column(conn, "library_files", "skip_objects_supported BOOLEAN NOT NULL DEFAULT 0")
    await add_column(conn, "print_archives", "skip_objects_supported BOOLEAN NOT NULL DEFAULT 0")


def _resolve(file_path: str) -> Path | None:
    if not file_path:
        return None
    p = Path(file_path)
    disk = p if p.is_absolute() else settings.base_dir / p
    return disk if disk.is_file() else None


def _read_3mf(disk: Path, plate_idx: int) -> tuple[bool, dict, list] | None:
    """``(skip_supported, printable_objects, plates)`` — None if unreadable."""
    from backend.app.services.archive import (
        discover_plate_objects,
        extract_skip_support_from_3mf,
        parse_plates_from_3mf,
    )

    try:
        data = disk.read_bytes()
        supported = extract_skip_support_from_3mf(data)
        with zipfile.ZipFile(disk, "r") as zf:
            objects = discover_plate_objects(zf, plate_idx)
            plates = parse_plates_from_3mf(zf)
        # JSON object keys are strings; the readers all int() them back.
        return supported, {str(k): v for k, v in objects.items()}, plates
    except Exception:
        return None


async def _backfill(db, *, table: str, json_column: str, plate_column: str | None) -> None:
    plate_select = f", {plate_column}" if plate_column else ""
    result = await db.execute(
        text(
            f"SELECT id, file_path, {json_column}{plate_select} FROM {table} "
            "WHERE file_path IS NOT NULL AND file_path != ''"
        )
    )
    rows = result.fetchall()
    total, updated, missing, unreadable = len(rows), 0, 0, 0
    pending = 0

    for row in rows:
        row_id, file_path, raw_json = row[0], row[1], row[2]
        plate_idx = (row[3] if plate_column else None) or 1

        try:
            meta = json.loads(raw_json) if isinstance(raw_json, str) else (raw_json or {})
        except (json.JSONDecodeError, TypeError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}

        disk = _resolve(file_path)
        if disk is None:
            # No file on disk: fall back to the flags m022 already stored. The
            # counts stay as they are — there is nothing to re-read them from.
            from backend.app.services.library_helpers import skip_objects_supported_from_metadata

            supported = skip_objects_supported_from_metadata(meta)
            await db.execute(
                text(f"UPDATE {table} SET skip_objects_supported = :s WHERE id = :id"),
                {"s": supported, "id": row_id},
            )
            missing += 1
        else:
            read = _read_3mf(disk, plate_idx)
            if read is None:
                unreadable += 1
                continue
            supported, objects, plates = read
            new_meta = dict(meta)
            # Only overwrite on a non-empty result: an unreadable plate must not
            # erase a value that was already right.
            if objects:
                new_meta["printable_objects"] = objects
            if plates:
                new_meta["plates"] = plates
                new_meta["is_multi_plate"] = len(plates) > 1
            await db.execute(
                text(f"UPDATE {table} SET skip_objects_supported = :s, {json_column} = :m WHERE id = :id"),
                {"s": supported, "m": json.dumps(new_meta), "id": row_id},
            )
            updated += 1

        pending += 1
        if pending >= _BATCH_SIZE:
            await db.commit()
            pending = 0
            logger.info(
                "m114 %s: progress %d/%d (updated=%d, missing=%d, unreadable=%d)",
                table,
                updated + missing + unreadable,
                total,
                updated,
                missing,
                unreadable,
            )
    if pending:
        await db.commit()
    logger.info(
        "m114 %s done: total=%d updated=%d missing=%d unreadable=%d",
        table,
        total,
        updated,
        missing,
        unreadable,
    )


async def seed(session_factory):
    async with session_factory() as db:
        await _backfill(db, table="library_files", json_column="file_metadata", plate_column=None)
        await _backfill(db, table="print_archives", json_column="extra_data", plate_column="plate_index")
