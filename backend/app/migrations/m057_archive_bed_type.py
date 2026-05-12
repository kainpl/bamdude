"""Build-plate type on print archives + on-disk backfill.

3MF metadata carries ``curr_bed_type`` (Cool Plate / Cool Plate SuperTack /
Engineering Plate / High Temp Plate / Textured PEI Plate / Smooth PEI Plate)
under ``Metadata/slice_info.config`` per-plate. The archive card surfaces
this so operators don't have to remember which plate matched a re-print —
the bed icon is rendered next to the printer / model name with the full
plate name in the hover tooltip.

Fresh installs get the column from ``backend/app/models/archive.py`` via
``Base.metadata.create_all``. Existing rows backfill to NULL — the card
omits the icon when ``bed_type`` is unset (no broken-image placeholder
on pre-feature archives). The ``seed()`` hook then walks every NULL row,
re-opens its on-disk 3MF, and fills the column from ``slice_info.config``
(primary, per-plate) with a ``project_settings.config`` fallback for
older 3MF shapes.

The backfill is **strictly best-effort** — same protocol as m056:

- Rows whose 3MF was auto-cleanup'd (``file_path`` empty or the file no
  longer on disk) are skipped silently. Operators can re-upload the
  source and hit per-archive **Rescan** to fill the column later.
- Per-row parse failures (corrupted ZIP, malformed XML) are logged at
  ``WARNING`` and swallowed; the migration completes either way.
- Idempotent re-runs (``DEBUG=true``) re-attempt only rows still at
  ``bed_type IS NULL``.

Adapted from upstream Bambuddy ``79d54a8d`` (#1253) — upstream shipped a
standalone ``scripts/backfill_archive_bed_type.py``; we fold it into the
migration so the upgrade does the right thing automatically without an
extra manual step.
"""

from __future__ import annotations

import json
import logging
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from sqlalchemy import select, update

from backend.app.migrations.helpers import add_column

logger = logging.getLogger(__name__)

version = 57
name = "archive_bed_type"


async def upgrade(conn):
    await add_column(conn, "print_archives", "bed_type VARCHAR(64)")


def _extract_bed_type(file_path: Path) -> str | None:
    """Pull ``curr_bed_type`` from a 3MF file. Returns the raw slicer string.

    Order of preference mirrors the live ``ThreeMFParser``: slice_info wins
    over project_settings because the former reflects the exported plate,
    while the latter is whatever the slicer happened to have selected at
    project save (per-plate vs project-wide).
    """
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            names = zf.namelist()

            # Primary source: slice_info.config (XML, per-plate metadata)
            if "Metadata/slice_info.config" in names:
                try:
                    root = ET.fromstring(zf.read("Metadata/slice_info.config").decode())
                    plate = root.find(".//plate")
                    if plate is not None:
                        for meta in plate.findall("metadata"):
                            if meta.get("key") == "curr_bed_type":
                                value = meta.get("value")
                                if value:
                                    return value.strip()
                except ET.ParseError:
                    pass

            # Fallback: project_settings.config (JSON, project-wide)
            if "Metadata/project_settings.config" in names:
                try:
                    data = json.loads(zf.read("Metadata/project_settings.config").decode())
                    val = data.get("curr_bed_type")
                    if isinstance(val, str) and val.strip():
                        return val.strip()
                except json.JSONDecodeError:
                    pass
    except (zipfile.BadZipFile, OSError):
        return None
    return None


async def seed(session_factory):
    """Best-effort backfill of ``bed_type`` from on-disk 3MF files.

    Late-import everything backend-side so migration discovery stays
    dependency-light (the loader pulls every migration's symbols at
    startup; pulling heavy modules here would slow boot for everyone
    regardless of whether the migration actually runs).
    """
    from backend.app.core.config import settings as app_settings
    from backend.app.models.archive import PrintArchive

    # Column-explicit read + Core update — see feedback_migration_seed_columns.
    # ``select(PrintArchive)`` would emit every model column in the SQL and
    # crash future upgrade chains that run this seed before a later
    # migration adds yet another archive column.
    async with session_factory() as session:
        result = await session.execute(
            select(PrintArchive.id, PrintArchive.file_path).where(PrintArchive.bed_type.is_(None))
        )
        rows = result.all()
        if not rows:
            logger.info("m057: no archives with bed_type=NULL — backfill skipped")
            return

        logger.info("m057: scanning %d archives for on-disk bed_type backfill", len(rows))

        updated = 0
        skipped_missing = 0
        skipped_no_value = 0

        for row in rows:
            if not row.file_path:
                skipped_missing += 1
                continue
            file_path = app_settings.base_dir / row.file_path
            if not file_path.exists():
                skipped_missing += 1
                continue

            bed_type = _extract_bed_type(file_path)
            if not bed_type:
                skipped_no_value += 1
                continue

            await session.execute(update(PrintArchive).where(PrintArchive.id == row.id).values(bed_type=bed_type))
            updated += 1

        if updated:
            try:
                await session.commit()
            except Exception as exc:
                logger.warning("m057: commit of %d bed_type updates failed: %s", updated, exc)
                await session.rollback()
                return

        logger.info(
            "m057: bed_type backfill — updated=%d, skipped_file_missing=%d, skipped_no_value=%d",
            updated,
            skipped_missing,
            skipped_no_value,
        )
