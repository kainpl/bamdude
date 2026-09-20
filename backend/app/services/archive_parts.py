"""Seeding and refreshing print_archive_parts — the live per-part plate state.

Called wherever an archive gains (or corrects) its 3MF: dispatch
(``archive_print``), external-print attach (``attach_3mf_to_archive``),
the adoption branch in main.py, the retry-download path (via attach) and
the backfill script. Best-effort by contract: any failure logs and seeds
nothing — a print must never fail because of the ledger. That includes
the file read itself: callers may pass a ``Path`` and let
``seed_archive_parts`` do the ``read_bytes()`` inside its own guard,
rather than reading at the call site where a transient I/O error would
raise out of the caller's own operation.
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


async def load_rows(db: AsyncSession, archive_id: int) -> list[PrintArchivePart]:
    """This archive's part rows in id order — the plate as everything reads it.

    One function because the order matters and is load-bearing: Telegram walks
    the rows by ascending id ("the next part"), the two wire shapes render them
    in that order, and the ledger's ``wanted`` map is built from the same walk.
    Four identical selects used to say so separately.
    """
    return list(
        (
            await db.execute(
                select(PrintArchivePart).where(PrintArchivePart.archive_id == archive_id).order_by(PrintArchivePart.id)
            )
        )
        .scalars()
        .all()
    )


async def seed_archive_parts(db: AsyncSession, archive: PrintArchive, data: bytes | Path) -> None:
    """(Re)build the archive's part rows from 3MF bytes.

    Replace, not merge — the plate may have changed (plate-corrected
    re-download). ``defective`` carries over by ``name_key`` for parts still
    present, capped at the new quantity; the flat ``defective_count`` is
    never lowered here.

    ``data`` may be raw bytes or a ``Path`` to the 3MF on disk. Passing a
    ``Path`` lets the ``.read_bytes()`` happen inside this function's own
    try/except, so a transient read error (missing/locked file, races with
    cleanup, …) is swallowed here rather than raised at the call site —
    where it would fail the caller's own operation (print dispatch, 3MF
    attach), violating the never-fail contract above.
    """
    try:
        from backend.app.services.archive import extract_printable_objects_from_3mf

        if isinstance(data, Path):
            data = data.read_bytes()

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

        new_rows = [
            PrintArchivePart(
                archive_id=archive.id,
                name=part.name,
                name_key=part.name_key,
                identify_ids=part.identify_ids,
                quantity=part.quantity,
                defective=min(old_defective.get(part.name_key, 0), part.quantity),
            )
            for part in tally_objects(objects)
        ]
        for row in new_rows:
            db.add(row)

        # ⚠️ A flat count typed BEFORE the rows existed must survive them.
        # An external print reaches ``completed`` before its 3MF arrives, so the
        # card and Telegram both offer the FLAT counter — and the rows seeded
        # afterwards were all born with ``defective = 0``, which reset
        # ``defective_count`` on the next write and let the free-stock credit
        # put the scrapped parts on the shelf. Same rule as m158's backfill: a
        # plate holding copies of exactly one part can adopt the number; a
        # multi-part plate cannot, and the mismatch is said out loud rather than
        # quietly resolved either way.
        if not old_rows and (archive.defective_count or 0) > 0:
            if apply_flat_defective(new_rows, int(archive.defective_count)):
                archive.defective_count = sum(r.defective or 0 for r in new_rows)
            else:
                logger.warning(
                    "seed_archive_parts: archive %s carries a flat defective_count of %s that its %d part row(s) "
                    "cannot adopt (a multi-part plate does not say which part went in the bin) — the flat count is "
                    "kept and the rows read 0; re-enter the defects per part",
                    archive.id,
                    archive.defective_count,
                    len(new_rows),
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
