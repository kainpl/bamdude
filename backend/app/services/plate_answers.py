"""One short, run-bound completion operation for web and Telegram plate answers."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass

from backend.app.models.archive import PrintArchive
from backend.app.services.archive_defects import DefectsResult, DefectsWrite, record_defects
from backend.app.services.plate_hold import (
    StalePlateAnswer,
    answer_by_clearing,
    answer_by_repeating,
    waiting_archive,
)

_printer_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


@dataclass(frozen=True)
class PlateAnswerResult:
    archive: PrintArchive | None
    action: str
    defects: DefectsResult | None = None
    item_id: int | None = None


async def answer_plate_run(
    db,
    *,
    printer_id: int,
    expected_archive_id: int | None,
    action: str,
    defects: DefectsWrite | None = None,
    actor_id: int | None = None,
) -> PlateAnswerResult:
    """Write defects and answer exactly the held run, then release the gate.

    The lock is intentionally process-local: BamDude has one scheduler and one
    Telegram poller in this process.  It closes the SQLite read-to-write window;
    the exact archive check remains the database-independent final guard.
    """
    if action not in {"clear", "repeat"}:
        raise ValueError(f"Unsupported plate answer: {action}")

    from backend.app.services.printer_manager import printer_manager

    async with _printer_locks[printer_id]:
        archive = await waiting_archive(db, printer_id)
        if expected_archive_id is not None and (archive is None or archive.id != expected_archive_id):
            raise StalePlateAnswer("This completion card is no longer current for this printer")
        if archive is None and defects is not None:
            raise StalePlateAnswer("No finished print is waiting on this printer")

        defects_result = None
        if defects is not None:
            if archive is None:
                raise StalePlateAnswer("No finished print is waiting on this printer")
            defects_result = await record_defects(db, archive, defects, actor_id=actor_id)

        item_id = None
        if action == "clear":
            await answer_by_clearing(db, printer_id, expected_archive_id=expected_archive_id, commit=False)
        else:
            row = await answer_by_repeating(db, printer_id, expected_archive_id=expected_archive_id, commit=False)
            if row is None:
                raise StalePlateAnswer("No finished print is waiting on this printer")
            item_id = row.id

        await db.commit()

    # The scheduler may run only after the database says which held row was answered.
    printer_manager.set_awaiting_plate_clear(printer_id, False)
    return PlateAnswerResult(archive=archive, action=action, defects=defects_result, item_id=item_id)
