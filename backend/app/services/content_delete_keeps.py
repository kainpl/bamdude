"""Physical print occupancy is independent of navigation ownership."""

from sqlalchemy import select

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import swap_terminal


async def archive_keep_reasons(db, archive, *, retired_printer_id=None):
    reasons = []
    retired = retired_printer_id is not None and archive.printer_id == retired_printer_id
    if not retired:
        if archive.status in {"printing", "paused", "dispatching"}:
            reasons.append("active_archive")
        if swap_terminal.claim_stage(archive.extra_data) == swap_terminal.CLAIM_OWED or swap_terminal.sending(
            archive.id
        ):
            reasons.append("swap_owed")
        if archive.printer_id is not None:
            from backend.app.services.print_run_binding import current_print_run, finishing_print_runs
            from backend.app.services.printer_manager import printer_manager

            bindings = (
                current_print_run(printer_manager, archive.printer_id),
                *finishing_print_runs(printer_manager, archive.printer_id),
            )
            if any(
                binding is not None
                and binding.archive_id == archive.id
                and binding.archive_identity in (None, archive.recovery_identity)
                for binding in bindings
            ):
                reasons.append("live_execution")
    gate = select(Printer.id).where(Printer.awaiting_plate_clear_archive_id == archive.id)
    occupied = (
        select(PrintQueueItem.id)
        .join(PrinterQueue, PrinterQueue.id == PrintQueueItem.queue_id)
        .where(
            PrintQueueItem.archive_id == archive.id,
            (PrintQueueItem.status.in_(("printing", "paused", "dispatching")))
            | (PrinterQueue.current_item_id == PrintQueueItem.id),
        )
    )
    if retired_printer_id is not None:
        gate = gate.where(Printer.id != retired_printer_id)
        occupied = occupied.where(PrinterQueue.printer_id != retired_printer_id)
    if await db.scalar(gate.limit(1)) is not None:
        reasons.append("plate_gate")
    if await db.scalar(occupied.limit(1)) is not None:
        reasons.append("live_job")
    return reasons
