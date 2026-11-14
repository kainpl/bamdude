"""Bounded DB enrichment for one fleet snapshot, shared with single-printer reads."""

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.plate_hold import waiting_row_predicate
from backend.app.services.print_execution import repeatable_predicate


async def current_archive_ids(db: AsyncSession, subtasks: dict[int, str | None]) -> dict[int, int | None]:
    if not subtasks:
        return {}
    # Same two newest-first lookups as the single-printer path: cloud subtask
    # first, then the open archive of a print started locally. Each subquery
    # returns at most one ID; never load the farm's whole archive history.
    subtask = case(subtasks, value=Printer.id)
    by_subtask = (
        select(PrintArchive.id)
        .where(PrintArchive.printer_id == Printer.id, PrintArchive.subtask_id == subtask, subtask.is_not(None))
        .order_by(PrintArchive.created_at.desc())
        .limit(1)
        .correlate(Printer)
        .scalar_subquery()
    )
    still_open = (
        select(PrintArchive.id)
        .where(PrintArchive.printer_id == Printer.id, PrintArchive.status == "printing")
        .order_by(PrintArchive.created_at.desc())
        .limit(1)
        .correlate(Printer)
        .scalar_subquery()
    )
    rows = await db.execute(select(Printer.id, func.coalesce(by_subtask, still_open)).where(Printer.id.in_(subtasks)))
    return dict(rows.all())


async def printers_with_waiting_rows(db: AsyncSession, printer_ids: list[int]) -> set[int]:
    if not printer_ids:
        return set()
    # Match the single-printer/WS reader without one query per held printer.
    rows = await db.scalars(
        select(PrinterQueue.printer_id)
        .join(PrintQueueItem, PrintQueueItem.queue_id == PrinterQueue.id)
        .join(Printer, PrinterQueue.printer_id == Printer.id)
        .where(PrinterQueue.printer_id.in_(printer_ids), waiting_row_predicate(), repeatable_predicate())
        .distinct()
    )
    return set(rows)
