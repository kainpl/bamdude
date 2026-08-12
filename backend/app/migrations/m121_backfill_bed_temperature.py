"""Fill in the bed temperature that every archive ever recorded is missing.

``PrintArchive.bed_temperature`` has been NULL on every row since the column
existed. The parser looked for ``bed_temperature`` /
``bed_temperature_initial_layer``, which exist in BambuStudio's config
*definitions* and are never written into an exported 3MF: the value lives per
plate type (``hot_plate_temp``, ``cool_plate_temp``, …) and ``curr_bed_type``
says which one applies. Nozzle temperature filled in fine throughout, because
``nozzle_temperature`` is a key that really is written — which is why only this
one field looked broken rather than the whole parser.

The reader is fixed, so new prints record it. This is the history: the 3MFs are
still on disk, so the value can be recovered rather than lost.

**Cost, and why it is paid here rather than lazily.** Every archive with a file
gets its 3MF opened once — roughly 50–200 ms each, so an installation with
thousands of archives sits for a few minutes on first start. m114 set that
precedent for the same reason: a one-off pass with progress in the log beats a
field that stays wrong until someone happens to open the row, and beats a
background task that can be interrupted halfway leaving no record of where it
stopped.

Only NULL rows are touched. A row that already has a temperature was either
written by the fixed parser or set by hand, and this must not overwrite either.
"""

from __future__ import annotations

import json
import logging
import zipfile
from pathlib import Path

from sqlalchemy import text

from backend.app.core.config import settings

logger = logging.getLogger(__name__)

version = 121
name = "backfill_bed_temperature"

_BATCH_SIZE = 200


async def upgrade(conn):
    """No schema change — the column has existed all along, just never filled."""


def _resolve(file_path: str) -> Path | None:
    """Archive paths are stored relative to ``settings.base_dir``; absolute ones
    appear on older rows. Mirrors ``m114._resolve``."""
    if not file_path:
        return None
    p = Path(file_path)
    disk = p if p.is_absolute() else settings.base_dir / p
    return disk if disk.is_file() else None


def _project_settings(disk: Path) -> dict | None:
    """The slicer config blob out of a 3MF, or None if it cannot be read.

    Fail-soft throughout: a truncated or non-ZIP file is a fact about that
    archive, not a reason to abort an upgrade. The row simply stays NULL, which
    is what it already was.
    """
    try:
        with zipfile.ZipFile(disk, "r") as zf:
            for entry in zf.namelist():
                if "project_settings" in entry and entry.endswith(".config"):
                    return json.loads(zf.read(entry))
    except Exception:  # noqa: BLE001 — see the docstring
        return None
    return None


async def seed(session_factory):
    from backend.app.services.archive import ThreeMFParser

    async with session_factory() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT id, file_path FROM print_archives "
                    "WHERE bed_temperature IS NULL AND file_path IS NOT NULL AND file_path != ''"
                )
            )
        ).fetchall()

        total = len(rows)
        if not total:
            return
        logger.info("m121: recovering bed temperature for %d archive(s) with a file on disk", total)

        updated = missing = unreadable = 0
        pending = 0

        for row_id, file_path in rows:
            disk = _resolve(file_path)
            if disk is None:
                missing += 1
                continue

            data = _project_settings(disk)
            if data is None:
                unreadable += 1
                continue

            # The same resolver the live parser uses, so history and new prints
            # cannot disagree about what a given file's bed temperature is.
            value = ThreeMFParser._bed_temperature_from(ThreeMFParser, data)  # noqa: SLF001
            if value is None:
                unreadable += 1
                continue

            await db.execute(
                text("UPDATE print_archives SET bed_temperature = :t WHERE id = :id"),
                {"t": value, "id": row_id},
            )
            updated += 1
            pending += 1

            if pending >= _BATCH_SIZE:
                await db.commit()
                pending = 0
                logger.info(
                    "m121 print_archives: progress %d/%d (updated=%d, missing=%d, unreadable=%d)",
                    updated + missing + unreadable,
                    total,
                    updated,
                    missing,
                    unreadable,
                )

        if pending:
            await db.commit()

        logger.info(
            "m121 done: total=%d updated=%d missing=%d unreadable=%d",
            total,
            updated,
            missing,
            unreadable,
        )
