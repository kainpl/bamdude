"""Queue rows nothing will ever clean up: superseded finished rows and orphan assignments.

A completed row is normally deleted the moment its print ends
(``plate_hold.clean_up_finished_row``). Two things keep it on purpose: a plate gate
that will ask the operator about it, and a held table swap -- the row is the hold's
record, and only a later successful swap of the same run cleans it
(``print_reconciliation._swap_settle``). Once ANOTHER print has started on that
printer neither can happen any more: the plate was dealt with physically, and an
owed swap is only ever sent while its execution still owns the queue. The row is
then an ordinary cleanup that was skipped, and its assigned AutoQueue partner hangs
with it. Found on a farm 2026-11-01: seven such pairs, ten days old.

This module is the one policy for that, used by the automatic passes (connect-edge
sweep, after a completion) and by the manual tool on the Info page. Retiring is the
ordinary cleanup -- ``detach_print_queue_refs``, delete, counters -- done under the
row's printer lane, after the policy is asked again on the locked state. Nothing is
sent to a printer, no effect is replayed, and the archive (with its ``swap_hold``
record) is history and stays as it is.

⚠️ Not operator print-state recovery (``print_state_recovery``): that one handles
ambiguous runs and refuses ``detach_print_queue_refs`` on principle. A superseded
finished row is not ambiguous -- a later print on the same machine settles it.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Select, and_, delete, exists, func, select
from sqlalchemy.orm import aliased

from backend.app.core import database
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import swap_terminal
from backend.app.services.print_state_recovery import OUTCOMES
from backend.app.services.queue_ops import claim_lanes

logger = logging.getLogger(__name__)

FINISHED_ROW = "finished_row"
ORPHAN_ASSIGNMENT = "orphan_assignment"

#: Why a superseded finished row is still kept -- each is a question somebody may yet answer.
KEEP_CURRENT_ITEM = "current_item"
KEEP_PLATE_GATE = "plate_gate"
KEEP_ARCHIVE_OPEN = "archive_open"
KEEP_SWAP_OWED = "swap_owed"
KEEP_ARCHIVE_COPY_MISSING = "archive_copy_missing"

#: Why a named id was not retired.
SKIP_GONE = "gone"
SKIP_CHANGED = "changed"
SKIP_NOT_FINISHED = "not_finished"
SKIP_NOT_SUPERSEDED = "not_superseded"
SKIP_NOT_ORPHAN = "not_orphan"


@dataclass(frozen=True)
class Leftover:
    kind: str
    item_id: int | None
    auto_item_id: int | None
    printer_id: int | None
    printer_name: str | None
    archive_id: int | None
    name: str | None
    completed_at: datetime | None
    hold_reason: str | None
    #: ``None`` when the row may be retired now.
    keep_reason: str | None


@dataclass(frozen=True)
class Skipped:
    kind: str
    item_id: int | None
    auto_item_id: int | None
    reason: str


@dataclass
class RetireResult:
    retired_items: list[int] = field(default_factory=list)
    retired_autos: list[int] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)


def _superseded_rows() -> Select:
    """Completed rows of a printer on which another print started after they finished.

    The one formula for "superseded": ``find`` lists it and ``retire`` asks it again
    under the lane. A later archive is any run on the same printer -- a dispatch claim
    included (an archive's start is its claim): from then on the earlier run no longer
    owns the queue.
    """
    later = aliased(PrintArchive)
    superseded = exists().where(
        later.printer_id == PrinterQueue.printer_id,
        later.id != PrintArchive.id,
        later.started_at > func.coalesce(PrintArchive.completed_at, PrintQueueItem.completed_at),
    )
    return (
        select(PrintQueueItem, PrintArchive, PrinterQueue, Printer)
        .join(PrintArchive, PrintArchive.id == PrintQueueItem.archive_id)
        .join(PrinterQueue, PrinterQueue.id == PrintQueueItem.queue_id)
        .join(Printer, Printer.id == PrinterQueue.printer_id)
        .where(PrintQueueItem.status == "completed", superseded)
        .order_by(PrintQueueItem.id)
    )


def _orphan_condition():
    """An assigned router row whose printer row is gone -- it carries nothing any more."""
    return and_(
        AutoQueueItem.status == "assigned",
        # A NULL reference cannot match a printer row either.
        ~exists().where(PrintQueueItem.id == AutoQueueItem.assigned_to_item_id),
    )


def _hold_reason(archive: PrintArchive) -> str | None:
    hold = (archive.extra_data or {}).get(swap_terminal.HOLD_KEY) if isinstance(archive.extra_data, dict) else None
    reason = hold.get("reason") if isinstance(hold, dict) else None
    return reason if isinstance(reason, str) else None


async def _keep_reason(db, row: PrintQueueItem, archive: PrintArchive, queue: PrinterQueue, printer: Printer):
    if queue.current_item_id == row.id:
        return KEEP_CURRENT_ITEM
    if printer.awaiting_plate_clear_archive_id == archive.id:
        return KEEP_PLATE_GATE
    if archive.status not in OUTCOMES:
        return KEEP_ARCHIVE_OPEN
    if swap_terminal.claim_stage(archive.extra_data) == swap_terminal.CLAIM_OWED or swap_terminal.sending(archive.id):
        return KEEP_SWAP_OWED
    if row.queue_source_id is not None:
        # The row may be the last owner of its captured bytes while the archive's own copy is still missing.
        from backend.app.services.queue_source_release import independent_archive_bytes

        if await independent_archive_bytes(db, archive.id) is not None:
            return KEEP_ARCHIVE_COPY_MISSING
    return None


async def find(db, *, printer_ids=None) -> list[Leftover]:
    """Every superseded finished row (with why it is still kept, if it is) and, unscoped, every orphan assignment.

    ``printer_ids`` narrows to those printers' queues; ``None`` is the whole farm. Orphan assignments have no
    printer, so a scoped caller never sees them.
    """
    query = _superseded_rows()
    if printer_ids is not None:
        query = query.where(PrinterQueue.printer_id.in_(list(printer_ids)))
    found: list[Leftover] = []
    for row, archive, queue, printer in (await db.execute(query)).all():
        found.append(
            Leftover(
                kind=FINISHED_ROW,
                item_id=row.id,
                auto_item_id=row.source_auto_item_id,
                printer_id=printer.id,
                printer_name=printer.name,
                archive_id=archive.id,
                name=archive.print_name or archive.filename,
                completed_at=archive.completed_at or row.completed_at,
                hold_reason=_hold_reason(archive),
                keep_reason=await _keep_reason(db, row, archive, queue, printer),
            )
        )
    if printer_ids is None:
        orphans = (await db.scalars(select(AutoQueueItem).where(_orphan_condition()).order_by(AutoQueueItem.id))).all()
        found.extend(
            Leftover(
                kind=ORPHAN_ASSIGNMENT,
                item_id=None,
                auto_item_id=auto.id,
                printer_id=None,
                printer_name=None,
                archive_id=auto.archive_id,
                name=None,
                completed_at=None,
                hold_reason=None,
                keep_reason=None,
            )
            for auto in orphans
        )
    return found


async def _retire_queue(queue_id: int, item_ids: list[int], result: RetireResult) -> int | None:
    """Retire this queue's named rows under its lane; the policy is asked again on the locked state."""
    from backend.app.services.queue_counters import detach_print_queue_refs, update_queue_counters

    retired: list[int] = []
    printer_id = None
    async with database.async_session() as db, claim_lanes(db, [queue_id]):
        candidates = {
            row.id: (row, archive, queue, printer)
            for row, archive, queue, printer in (
                await db.execute(
                    _superseded_rows().where(PrintQueueItem.id.in_(item_ids)).execution_options(populate_existing=True)
                )
            ).all()
        }
        for item_id in item_ids:
            if item_id not in candidates:
                row = await db.get(PrintQueueItem, item_id, populate_existing=True)
                reason = (
                    SKIP_GONE
                    if row is None
                    else SKIP_CHANGED
                    if row.queue_id != queue_id
                    else SKIP_NOT_FINISHED
                    if row.status != "completed"
                    else SKIP_NOT_SUPERSEDED
                )
                result.skipped.append(Skipped(FINISHED_ROW, item_id, None, reason))
                continue
            row, archive, queue, printer = candidates[item_id]
            if row.queue_id != queue_id:
                result.skipped.append(Skipped(FINISHED_ROW, item_id, None, SKIP_CHANGED))
                continue
            keep = await _keep_reason(db, row, archive, queue, printer)
            if keep is not None:
                result.skipped.append(Skipped(FINISHED_ROW, item_id, None, keep))
                continue
            printer_id = printer.id
            await detach_print_queue_refs(db, [row.id])
            await db.delete(row)
            retired.append(item_id)
        if retired:
            await update_queue_counters(db, queue_id)
        await db.commit()
    if retired:
        result.retired_items.extend(retired)
        logger.info("Queue leftovers: retired superseded finished rows %s of queue %s", retired, queue_id)
    return printer_id if retired else None


async def _retire_orphan(auto_id: int, result: RetireResult) -> None:
    async with database.async_session() as db:
        deleted = await db.execute(
            delete(AutoQueueItem)
            .where(AutoQueueItem.id == auto_id, _orphan_condition())
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        if deleted.rowcount:
            result.retired_autos.append(auto_id)
            logger.info("Queue leftovers: retired orphan assignment %s", auto_id)
            return
        reason = SKIP_GONE if await db.get(AutoQueueItem, auto_id) is None else SKIP_NOT_ORPHAN
        result.skipped.append(Skipped(ORPHAN_ASSIGNMENT, None, auto_id, reason))


async def retire(*, item_ids=(), auto_item_ids=()) -> RetireResult:
    """Retire the named rows that the policy still allows; report the rest with the reason.

    Each queue is handled in a clean session under its printer lane -- the order dispatch, transfer and
    cancel/delete keep -- so a row claimed, moved or answered meanwhile is read again and skipped.
    """
    result = RetireResult()
    item_ids = sorted(set(item_ids))
    if item_ids:
        async with database.async_session() as preflight:
            lanes = dict(
                (
                    await preflight.execute(
                        select(PrintQueueItem.id, PrintQueueItem.queue_id).where(PrintQueueItem.id.in_(item_ids))
                    )
                ).all()
            )
        by_queue: dict[int, list[int]] = defaultdict(list)
        for item_id in item_ids:
            if item_id in lanes:
                by_queue[lanes[item_id]].append(item_id)
            else:
                result.skipped.append(Skipped(FINISHED_ROW, item_id, None, SKIP_GONE))
        changed_printers = []
        for queue_id in sorted(by_queue):
            printer_id = await _retire_queue(queue_id, by_queue[queue_id], result)
            if printer_id is not None:
                changed_printers.append(printer_id)
        if changed_printers:
            from backend.app.core.websocket import ws_manager

            for printer_id in changed_printers:
                await ws_manager.send_queue_changed(printer_id)
    for auto_id in sorted(set(auto_item_ids)):
        await _retire_orphan(auto_id, result)
    return result


async def retire_for_printer(printer_id: int) -> RetireResult:
    """The automatic pass for one printer: retire what the manual tool would. Never raises."""
    try:
        async with database.async_session() as db:
            found = await find(db, printer_ids={printer_id})
        ids = [leftover.item_id for leftover in found if leftover.keep_reason is None]
        if not ids:
            return RetireResult()
        return await retire(item_ids=ids)
    except Exception:  # noqa: BLE001 -- a background cleanup must never fail the path that called it
        logger.exception("Queue leftovers: automatic pass failed for printer %s", printer_id)
        return RetireResult()
