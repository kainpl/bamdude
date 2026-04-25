"""Backfill the per-plate ``plates[]`` cache into JSON metadata.

Both ``library_files.file_metadata`` and ``print_archives.extra_data``
gain two top-level keys:

* ``plates`` — the same per-plate dict list that the
  ``/library/files/{id}/plates`` and ``/archives/{id}/plates`` endpoints
  return (minus ``thumbnail_url`` — the endpoint composes that on the
  fly so the cached payload doesn't go stale if base URLs change).
* ``is_multi_plate`` — convenience boolean (``len(plates) > 1``) so the
  file-list / archive-list response can gate gallery rendering on the
  frontend without an extra fetch per single-plate file.

Forward path (new uploads + new prints): the upload route +
``ArchiveService.archive_print()`` / ``attach_3mf_to_archive()`` already
populate both fields after this migration ships. This file only
backfills installations that already had files / archives before 0.4.1.

**Long-startup warning** (mirror of m022): on installs with hundreds or
thousands of 3MFs this seed step opens each ZIP, parses XML/JSON,
serialises to JSON, merges into the row. Expect 50-200 ms per file.
CHANGELOG entry mentions the one-time delay.
"""

from __future__ import annotations

import json
import logging
import zipfile
from pathlib import Path

from sqlalchemy import text

from backend.app.core.config import settings

logger = logging.getLogger(__name__)

version = 23
name = "per_plate_metadata_backfill"


# Commit batch size — keeps the SQLite write-lock window short so a
# parallel reader (e.g. a startup ws_manager subscriber) doesn't time
# out. Tuned for "long but progressable" rather than "one big txn".
_BATCH_SIZE = 100


def _extract_plates(file_path: Path) -> list[dict] | None:
    """Open *file_path* (3MF) and return the ``plates[]`` payload.

    Imports the parser locally — migrations are frozen and shouldn't
    pin themselves to live service code at module-load time, but this
    one specifically delegates to ``services.archive`` since the
    parsing logic is intricate and needs to stay in lockstep with the
    upload + endpoint paths. Acceptable trade-off because m023 sits at
    the very end of the chain — by the time it runs the parser is
    settled for that release.

    Returns None when the 3MF can't be opened. Returns ``[]`` when no
    plate metadata is found (still serialised — the ``is_multi_plate``
    flag will be False).
    """
    try:
        from backend.app.services.archive import parse_plates_from_3mf

        with zipfile.ZipFile(file_path, "r") as zf:
            return parse_plates_from_3mf(zf)
    except (zipfile.BadZipFile, OSError):
        return None
    except Exception as e:  # noqa: BLE001 — log + skip is the design
        logger.debug("m023: unexpected error reading %s: %s", file_path, e)
        return None


async def upgrade(conn):
    """No DDL — both target columns already exist; we only fill JSON."""
    _ = conn  # noqa: ARG001


async def seed(session_factory):
    """Backfill JSON metadata in library_files + print_archives."""
    async with session_factory() as db:
        # ---------- library_files.file_metadata ----------
        result = await db.execute(
            text("SELECT id, file_path, file_metadata FROM library_files WHERE file_metadata IS NOT NULL")
        )
        rows = result.fetchall()
        lib_total = len(rows)
        lib_updated = 0
        lib_already = 0
        lib_missing_file = 0
        lib_unreadable = 0
        pending = 0

        for row_id, file_path, file_metadata in rows:
            try:
                meta = json.loads(file_metadata) if isinstance(file_metadata, str) else (file_metadata or {})
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(meta, dict):
                continue
            if "plates" in meta and "is_multi_plate" in meta:
                lib_already += 1
                continue
            if not file_path:
                lib_missing_file += 1
                continue
            disk_path = Path(file_path) if Path(file_path).is_absolute() else settings.base_dir / file_path
            if not disk_path.is_file() or not str(disk_path).lower().endswith(".3mf"):
                lib_missing_file += 1
                continue
            plates_payload = _extract_plates(disk_path)
            if plates_payload is None:
                lib_unreadable += 1
                continue
            new_meta = {**meta, "plates": plates_payload, "is_multi_plate": len(plates_payload) > 1}
            await db.execute(
                text("UPDATE library_files SET file_metadata = :m WHERE id = :id"),
                {"m": json.dumps(new_meta), "id": row_id},
            )
            lib_updated += 1
            pending += 1
            if pending >= _BATCH_SIZE:
                await db.commit()
                pending = 0
                logger.info(
                    "m023 library_files: progress %d/%d (updated=%d, missing=%d, unreadable=%d)",
                    lib_already + lib_updated + lib_missing_file + lib_unreadable,
                    lib_total,
                    lib_updated,
                    lib_missing_file,
                    lib_unreadable,
                )
        if pending:
            await db.commit()
            pending = 0
        logger.info(
            "m023 library_files done: total=%d updated=%d already=%d missing=%d unreadable=%d",
            lib_total,
            lib_updated,
            lib_already,
            lib_missing_file,
            lib_unreadable,
        )

        # ---------- print_archives.extra_data ----------
        result = await db.execute(
            text(
                "SELECT id, file_path, extra_data FROM print_archives "
                "WHERE file_path IS NOT NULL AND file_path != '' "
                "AND extra_data IS NOT NULL"
            )
        )
        rows = result.fetchall()
        arch_total = len(rows)
        arch_updated = 0
        arch_already = 0
        arch_missing_file = 0
        arch_unreadable = 0

        for row_id, file_path, extra_data in rows:
            try:
                meta = json.loads(extra_data) if isinstance(extra_data, str) else (extra_data or {})
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(meta, dict):
                continue
            if "plates" in meta and "is_multi_plate" in meta:
                arch_already += 1
                continue
            disk_path = settings.base_dir / file_path
            if not disk_path.is_file() or not str(disk_path).lower().endswith(".3mf"):
                arch_missing_file += 1
                continue
            plates_payload = _extract_plates(disk_path)
            if plates_payload is None:
                arch_unreadable += 1
                continue
            new_meta = {**meta, "plates": plates_payload, "is_multi_plate": len(plates_payload) > 1}
            await db.execute(
                text("UPDATE print_archives SET extra_data = :m WHERE id = :id"),
                {"m": json.dumps(new_meta), "id": row_id},
            )
            arch_updated += 1
            pending += 1
            if pending >= _BATCH_SIZE:
                await db.commit()
                pending = 0
                logger.info(
                    "m023 print_archives: progress %d/%d (updated=%d, missing=%d, unreadable=%d)",
                    arch_already + arch_updated + arch_missing_file + arch_unreadable,
                    arch_total,
                    arch_updated,
                    arch_missing_file,
                    arch_unreadable,
                )
        if pending:
            await db.commit()
        logger.info(
            "m023 print_archives done: total=%d updated=%d already=%d missing=%d unreadable=%d",
            arch_total,
            arch_updated,
            arch_already,
            arch_missing_file,
            arch_unreadable,
        )
