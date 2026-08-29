"""Seeding and refreshing print_archive_parts — the live per-part plate state.

Called wherever an archive gains (or corrects) its 3MF: dispatch
(``archive_print``), external-print attach (``attach_3mf_to_archive``),
the adoption branch in main.py, the retry-download path (via attach) and
the backfill script. Best-effort by contract: any failure logs and seeds
nothing — a print must never fail because of the ledger.
"""

import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings
from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart
from backend.app.services.part_names import tally_objects

logger = logging.getLogger(__name__)


async def seed_archive_parts(db: AsyncSession, archive: PrintArchive, data: bytes) -> None:
    """(Re)build the archive's part rows from 3MF bytes.

    Replace, not merge — the plate may have changed (plate-corrected
    re-download). ``defective`` carries over by ``name_key`` for parts still
    present, capped at the new quantity; the flat ``defective_count`` is
    never lowered here.
    """
    try:
        from backend.app.services.archive import extract_printable_objects_from_3mf

        objects = extract_printable_objects_from_3mf(data, plate_number=archive.plate_index)
        if not isinstance(objects, dict) or not objects:
            return

        old_rows = (
            (await db.execute(select(PrintArchivePart).where(PrintArchivePart.archive_id == archive.id)))
            .scalars()
            .all()
        )
        old_defective = {r.name_key: r.defective for r in old_rows}
        for row in old_rows:
            await db.delete(row)

        for part in tally_objects(objects):
            db.add(
                PrintArchivePart(
                    archive_id=archive.id,
                    name=part.name,
                    name_key=part.name_key,
                    identify_ids=part.identify_ids,
                    quantity=part.quantity,
                    defective=min(old_defective.get(part.name_key, 0), part.quantity),
                )
            )
    except Exception as e:  # noqa: BLE001 — the ledger must never fail a print
        logger.warning("seed_archive_parts failed for archive %s: %s", archive.id, e)


async def refresh_archive_parts(archive_id: int) -> None:
    """Self-contained re-seed from the archive's file on disk.

    Opens its own session — for callers outside a request (adoption
    branches, backfill). No file, no rows, no error.
    """
    from backend.app.core.database import async_session

    try:
        async with async_session() as db:
            archive = (await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))).scalar_one_or_none()
            if archive is None or not archive.file_path:
                return
            path = Path(archive.file_path)
            if not path.is_absolute():
                path = settings.base_dir / archive.file_path
            if not path.is_file():
                return
            await seed_archive_parts(db, archive, path.read_bytes())
            await db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("refresh_archive_parts failed for archive %s: %s", archive_id, e)


def apply_flat_defective(rows: list[PrintArchivePart], flat: int) -> bool:
    """Backfill rule: a plate holding copies of exactly ONE part can adopt the
    legacy flat defective_count as that part's scrap (capped at quantity).
    A multi-part plate cannot — we don't know which part went in the bin.
    """
    if flat <= 0 or len(rows) != 1:
        return False
    rows[0].defective = min(flat, rows[0].quantity)
    return True
