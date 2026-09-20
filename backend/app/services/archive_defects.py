"""The one writer of what came out bad on a printed plate.

``PrintArchive.defective_count`` and ``print_archive_parts.defective`` used to
be written in the archive route's body and read by three things that subtract
them — the order's figures, the shelf credit, the statistics. Four doors now
write them (the archive editor, the order page, the two plate answers, the
Telegram prompt), so the rules live here once:

* a per-part value is ABSOLUTE and clamped to the row's quantity; a row id that
  is not this archive's is ignored, not an error (the PATCH route's rule since
  m158);
* ``defective_count`` is the SUM of the rows when rows exist, else the clamped
  flat value — a multi-part plate's flat count is never typed;
* **the shelf follows**: a completed, order-less print with a library file was
  credited at completion as ``printed − defective``, and a defect recorded the
  next morning would otherwise leave the shelf holding parts that are in the
  bin — see :func:`part_stock.adjust_unfiled_print`. A refusal there (the parts
  were already spent) never refuses the defects: the archive is the print
  history and must say what came out bad; the shelf is corrected by hand.

Flushes; the caller commits, by its router's convention.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart
from backend.app.services import part_stock
from backend.app.services.archive_parts import load_rows

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DefectsWrite:
    """What a door asked to record: ``(row id, defective)`` pairs, or one flat
    count for an archive that has no part rows."""

    parts: tuple[tuple[int, int], ...] = ()
    flat: int | None = None


@dataclass
class DefectsResult:
    defective_count: int
    parts: list[PrintArchivePart]
    #: ``(product_part_id, delta)`` written to the shelf.
    ledger_adjusted: list[tuple[int, int]] = field(default_factory=list)
    #: Product part ids whose correction the ledger refused (stock already spent).
    ledger_refused: list[int] = field(default_factory=list)


async def record_defects(
    db: AsyncSession, archive: PrintArchive, write: DefectsWrite, *, actor_id: int | None = None
) -> DefectsResult:
    """Record scrap on ``archive`` and bring the shelf in line — see the module docstring."""
    rows = await load_rows(db, archive.id)
    if rows:
        by_id = {row.id: row for row in rows}
        for row_id, defective in write.parts:
            row = by_id.get(row_id)
            if row is not None:
                row.defective = max(0, min(int(defective), int(row.quantity or 0)))
        archive.defective_count = sum(row.defective or 0 for row in rows)
    elif write.flat is not None:
        archive.defective_count = max(0, min(int(write.flat), int(archive.quantity or 0)))
    await db.flush()

    result = DefectsResult(defective_count=int(archive.defective_count or 0), parts=rows)
    adjusted = await part_stock.adjust_unfiled_print(
        db, archive, note=part_stock.NOTE_DEFECTS_RECORDED, created_by=actor_id
    )
    result.ledger_adjusted = [(m.product_part_id, m.delta) for m in adjusted.written]
    result.ledger_refused = list(adjusted.refused)
    if adjusted.refused:
        logger.warning(
            "archive %s: defects recorded (%d) but the shelf could not follow on part(s) %s — already spent; "
            "correct by hand from the product page",
            archive.id,
            result.defective_count,
            ", ".join(str(p) for p in adjusted.refused),
        )
    return result
