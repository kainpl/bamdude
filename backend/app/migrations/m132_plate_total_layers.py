"""Refresh ``library_files.file_metadata['plates']`` so each plate carries its
own layer count.

``parse_plates_from_3mf`` now reads ``; total layer number: N`` out of each
plate's own g-code. Rows written before that do not have the key, and nothing
recomputes the cache on read — so without this pass the field exists only for
files imported from here on.

⚠️ **Per plate, not per file, and that is the whole reason it lives here.**
Plate 1 of a container can be 200 layers and plate 5 eighty; a single number on
``library_files`` would be a guess dressed as a fact, which is why no column was
added for it. The number is read from the plate the caller names.

⚠️ **Never erases.** ``plates`` is overwritten only when the re-parse returned a
non-empty list, so an unreadable or since-deleted 3MF leaves the previous cache
alone rather than replacing good data with nothing. Same rule as m114.

Importing ``parse_plates_from_3mf`` from ``services.archive`` is deliberate, for
the reason m023, m036 and m114 give: this backfills a *derived cache*, where a
later extractor producing a better result is the goal. The copy-the-helper rule
guards frozen semantics, and a cache has none.

⚠️ **Archives are deliberately not touched.** ``print_archives`` already carries
a file-level ``total_layers`` column filled from the printed plate, so it has
the number it needs; its per-plate cache picks the key up whenever it is next
rebuilt. Rebuilding thousands of archive 3MFs for a field nothing reads there
would be minutes of startup bought for nothing.

**Long-startup warning**: opens every library 3MF still on disk. Each open reads
only the first 4 KB of each plate's g-code, so it is far cheaper than m114's
pass, but on a large library it is still measurable. Progress is logged every
100 rows.
"""

from __future__ import annotations

import json
import logging
import zipfile
from pathlib import Path

from sqlalchemy import text

from backend.app.core.config import settings

logger = logging.getLogger(__name__)

version = 132
name = "plate_total_layers"


# Keeps the SQLite write-lock window short so a parallel reader does not time
# out. Same value and rationale as m022 and m114.
_BATCH_SIZE = 100


async def upgrade(conn):
    """No DDL. The layer count lives inside an existing JSON column — see the
    module docstring for why it is not a column of its own."""


def _resolve(file_path: str) -> Path | None:
    if not file_path:
        return None
    p = Path(file_path)
    disk = p if p.is_absolute() else settings.base_dir / p
    return disk if disk.is_file() else None


def _read_plates(disk: Path) -> list | None:
    from backend.app.services.archive import parse_plates_from_3mf

    try:
        with zipfile.ZipFile(disk, "r") as zf:
            return parse_plates_from_3mf(zf)
    except Exception:
        return None


async def seed(session_factory):
    async with session_factory() as db:
        result = await db.execute(
            text(
                "SELECT id, file_path, file_metadata FROM library_files WHERE file_path IS NOT NULL AND file_path != ''"
            )
        )
        rows = result.fetchall()
        total, updated, skipped, unreadable = len(rows), 0, 0, 0
        pending = 0

        for row_id, file_path, raw_json in rows:
            try:
                meta = json.loads(raw_json) if isinstance(raw_json, str) else (raw_json or {})
            except (json.JSONDecodeError, TypeError):
                meta = {}
            if not isinstance(meta, dict):
                meta = {}

            disk = _resolve(file_path)
            if disk is None:
                # Nothing to re-read from. The cache stays as it is.
                skipped += 1
                continue

            plates = _read_plates(disk)
            if not plates:
                unreadable += 1
                continue

            new_meta = dict(meta)
            new_meta["plates"] = plates
            new_meta["is_multi_plate"] = len(plates) > 1
            await db.execute(
                text("UPDATE library_files SET file_metadata = :m WHERE id = :id"),
                {"m": json.dumps(new_meta), "id": row_id},
            )
            updated += 1

            pending += 1
            if pending >= _BATCH_SIZE:
                await db.commit()
                pending = 0
                logger.info(
                    "m132 library_files: progress %d/%d (updated=%d, skipped=%d, unreadable=%d)",
                    updated + skipped + unreadable,
                    total,
                    updated,
                    skipped,
                    unreadable,
                )

        if pending:
            await db.commit()
        logger.info(
            "m132 library_files done: total=%d updated=%d skipped=%d unreadable=%d",
            total,
            updated,
            skipped,
            unreadable,
        )
