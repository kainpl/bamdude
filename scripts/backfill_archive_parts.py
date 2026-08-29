"""Manual re-run: seed print_archive_parts for every archive with a 3MF on disk.

First population now happens automatically, on upgrade, in m158's ``seed()``
(``backend/app/migrations/m158_parts_ledger.py``) — every user gets the
ledger backfilled the moment they migrate through it, since users upgrade
through migrations only. This script is NOT part of that path; it stays
for MANUAL re-runs after a canonicalisation-rule change (``services/
part_names.py``) or for troubleshooting a specific install.

Run with the backend STOPPED (or against a copy of DATA_DIR):

    python -m scripts.backfill_archive_parts [--dry-run]

Skips archives that already have part rows. After seeding, the legacy flat
``defective_count`` is attributed to the part on mono-part plates
(``apply_flat_defective``); multi-part plates keep it unattributed.
"""

import argparse
import asyncio
import contextlib
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from backend.app.core.config import settings  # noqa: E402
from backend.app.core.database import async_session  # noqa: E402
from backend.app.models.archive import PrintArchive  # noqa: E402
from backend.app.models.archive_part import PrintArchivePart  # noqa: E402
from backend.app.services.archive_parts import apply_flat_defective, seed_archive_parts  # noqa: E402

logger = logging.getLogger(__name__)


async def main(dry_run: bool) -> None:
    seeded = attributed = skipped = missing = no_parts = failed = 0
    async with async_session() as db:
        archives = (
            (
                await db.execute(
                    select(PrintArchive).where(PrintArchive.file_path != "", PrintArchive.deleted_at.is_(None))
                )
            )
            .scalars()
            .all()
        )
        have_rows = {
            archive_id for (archive_id,) in (await db.execute(select(PrintArchivePart.archive_id).distinct())).all()
        }
        for archive in archives:
            if archive.id in have_rows:
                skipped += 1
                continue
            path = Path(archive.file_path)
            if not path.is_absolute():
                path = settings.base_dir / archive.file_path
            if not path.is_file():
                missing += 1
                continue
            try:
                await seed_archive_parts(db, archive, path.read_bytes())
                rows = (
                    (await db.execute(select(PrintArchivePart).where(PrintArchivePart.archive_id == archive.id)))
                    .scalars()
                    .all()
                )
                has_rows = bool(rows)
                did_attribute = has_rows and apply_flat_defective(rows, archive.defective_count or 0)
                if not dry_run:
                    await db.commit()
                if has_rows:
                    seeded += 1
                    if did_attribute:
                        attributed += 1
                else:
                    no_parts += 1
            except Exception as e:  # noqa: BLE001
                with contextlib.suppress(Exception):
                    await db.rollback()
                failed += 1
                logger.warning("Failed to backfill archive %s: %s", archive.id, e)
        if dry_run:
            await db.rollback()
    print(
        f"seeded={seeded} defect_attributed={attributed} already_had_rows={skipped} file_missing={missing} "
        f"no_parts={no_parts} failed={failed}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.dry_run))
