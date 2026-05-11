#!/usr/bin/env python3
"""Backfill bed_type on existing archives from their on-disk 3MF files.

Newly-ingested archives capture ``curr_bed_type`` at parse time (m057 + the
``ThreeMFParser`` hook). Archives created before this column existed have
``bed_type=NULL``, so their cards / list rows don't render the build-plate
icon next to the printer / model name. This script walks every NULL row,
re-opens the on-disk 3MF (via ``settings.base_dir / archive.file_path``),
and populates ``bed_type`` from ``Metadata/slice_info.config`` (per-plate,
authoritative) with a fallback to ``Metadata/project_settings.config`` for
older 3MF shapes.

Safe to re-run: filters on ``bed_type IS NULL``, so already-backfilled rows
are skipped. Archives whose 3MF was auto-cleanup'd (``file_path`` empty or
the file no longer on disk) are reported as ``skipped (file missing)``;
those rows can still be re-populated later via the per-archive **Rescan**
button if you re-upload the source 3MF.

Usage::

    # From the bamdude directory:
    python scripts/backfill_archive_bed_type.py

    # Inside the Docker container:
    docker exec -it bamdude python scripts/backfill_archive_bed_type.py

    # Preview without writing:
    python scripts/backfill_archive_bed_type.py --dry-run

Adapted from upstream Bambuddy ``79d54a8d`` (#1253).
"""

import argparse
import asyncio
import json
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

# Add parent directory to path for imports.
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select  # noqa: E402

from backend.app.core.config import settings  # noqa: E402
from backend.app.core.database import async_session, init_db  # noqa: E402
from backend.app.models.archive import PrintArchive  # noqa: E402


def _describe_db() -> str:
    """Redact credentials from ``DATABASE_URL`` for safe display."""
    url = settings.database_url
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if "@" in rest:
        creds, host = rest.split("@", 1)
        user = creds.split(":", 1)[0]
        return f"{scheme}://{user}:***@{host}"
    return url


def extract_bed_type(file_path: Path) -> str | None:
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


async def backfill(dry_run: bool = False) -> None:
    print("=" * 60)
    print("Archive bed_type backfill")
    print("=" * 60)
    print(f"Database: {_describe_db()}")
    print()
    if dry_run:
        print("DRY RUN MODE - No changes will be written")
        print()

    # Ensure the column exists before querying. ``init_db`` is idempotent and
    # is what the backend runs at every startup, so this is safe against a
    # live DB — fresh-install path goes through ``Base.metadata.create_all``,
    # upgrade path through the m057 migration.
    await init_db()

    async with async_session() as db:
        result = await db.execute(select(PrintArchive).where(PrintArchive.bed_type.is_(None)))
        archives = result.scalars().all()
        print(f"Found {len(archives)} archives with bed_type=NULL")
        print()

        updated = 0
        skipped_missing = 0
        skipped_no_value = 0

        for archive in archives:
            if not archive.file_path:
                skipped_missing += 1
                continue
            file_path = settings.base_dir / archive.file_path
            if not file_path.exists():
                skipped_missing += 1
                continue

            bed_type = extract_bed_type(file_path)
            if not bed_type:
                skipped_no_value += 1
                continue

            print(f"  [{archive.id}] {archive.print_name or archive.filename}: -> {bed_type}")
            if not dry_run:
                archive.bed_type = bed_type
            updated += 1

        if not dry_run:
            await db.commit()

        print()
        print("-" * 60)
        print(f"Updated: {updated}")
        print(f"Skipped (file missing): {skipped_missing}")
        print(f"Skipped (no bed_type in 3MF): {skipped_no_value}")
        if dry_run and updated:
            print()
            print("Run without --dry-run to apply.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill bed_type on existing archives from on-disk 3MF files.")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without writing")
    args = parser.parse_args()
    asyncio.run(backfill(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
