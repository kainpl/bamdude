"""Read-only, bounded-query projection. Never asks the scheduler to run."""

from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import get_args

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only, selectinload

from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.schemas.monitor import (
    DispatchPhase,
    MonitorCapabilities,
    MonitorDispatch,
    MonitorHMS,
    MonitorJob,
    MonitorPrinter,
    MonitorQueue,
    MonitorSnapshot,
    MonitorView,
    MonitorWait,
    RestrictedJob,
)
from backend.app.services.background_dispatch import background_dispatch
from backend.app.services.filament_intake import loaded_descriptor, source_display_filename
from backend.app.services.hms_errors import PAUSE_REASON_LABELS
from backend.app.services.printer_manager import display_temperatures, get_derived_status_name, printer_manager
from backend.app.services.queue_wait_reason import WaitCode
from backend.app.services.source_io import SourceUnavailable


@dataclass(frozen=True)
class MonitorAccess:
    queue_read: bool = False
    read_all: bool = False
    read_own: bool = False
    user_id: int | None = None
    kiosk: bool = False
    open_printer: bool = False
    open_queue: bool = False

    def visible(self, item: PrintQueueItem) -> bool:
        return not self.kiosk and (
            self.read_all or (self.read_own and self.user_id is not None and item.created_by_id == self.user_id)
        )


def _utc(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def _job(item: PrintQueueItem, access: MonitorAccess):
    """What the wall calls a job.

    ⚠️ The job's OWN snapshot is the last answer, not the first (m173, spec §4):
    exactly the precedence ``print_queue._enrich_response`` uses, so the wall and
    the queue card cannot name one job two ways. Without it a job whose library
    row or archive has been deleted — which still prints perfectly from its own
    copy — appeared on the wall as a nameless tile. ``source_display_filename``
    refuses to hand over the object's hash as a name (§4, A04), and a job it
    cannot name stays nameless rather than showing one.
    """
    if not access.visible(item):
        return RestrictedJob()
    name = (item.archive.print_name or item.archive.filename) if item.archive else None
    if not name and item.library_file:
        name = item.library_file.filename
    if not name:
        descriptor = loaded_descriptor(item)
        if descriptor is not None:
            with suppress(SourceUnavailable):
                name = source_display_filename(descriptor)
    return MonitorJob(name=name, item_id=item.id)


def _waiting(item: PrintQueueItem | None, printer: MonitorPrinter, access: MonitorAccess, now: datetime):
    if not item:
        return None
    # Kiosks may see generic operational reasons, never item-owned arbitrary
    # messages. An ownership-limited signed-in reader cannot inspect that row.
    if not access.kiosk and not access.visible(item):
        return None
    if not printer.connected:
        return MonitorWait(code="printer_offline")
    if printer.require_plate_clear and printer.awaiting_plate_clear and printer.state not in ("RUNNING", "PAUSE"):
        return MonitorWait(code="plate_not_cleared")
    scheduled = _utc(item.scheduled_time)
    if scheduled and scheduled > now:
        return MonitorWait(code="scheduled", until=scheduled)
    if item.manual_start:
        return MonitorWait(code="manual_start")
    if printer.state in ("RUNNING", "PAUSE") or printer.dispatch:
        return None
    if item.waiting_reason_code in get_args(WaitCode):
        return MonitorWait(code=item.waiting_reason_code, since=_utc(item.waiting_reason_checked_at))
    if item.waiting_reason:
        return MonitorWait(code="unknown")
    return None


async def build_snapshot(db: AsyncSession, view: MonitorView, access: MonitorAccess) -> MonitorSnapshot:
    now = datetime.now(timezone.utc)
    printers = list((await db.scalars(select(Printer).where(Printer.archived.is_(False)).order_by(Printer.name))).all())
    ids = [p.id for p in printers]
    queues = {}
    counts = {}
    heads = {}
    if ids and access.queue_read:
        queues = {
            q.printer_id: q for q in (await db.scalars(select(PrinterQueue).where(PrinterQueue.printer_id.in_(ids))))
        }
        counts = dict(
            (
                await db.execute(
                    select(PrintQueueItem.queue_id, func.count())
                    .where(
                        PrintQueueItem.queue_id.in_(ids),
                        PrintQueueItem.status == "pending",
                    )
                    .group_by(PrintQueueItem.queue_id)
                )
            ).all()
        )
        ranked = (
            select(
                PrintQueueItem.id,
                func.row_number()
                .over(
                    partition_by=(PrintQueueItem.queue_id, PrintQueueItem.status),
                    order_by=(PrintQueueItem.position, PrintQueueItem.id),
                )
                .label("rank"),
            )
            .where(
                PrintQueueItem.queue_id.in_(ids),
                PrintQueueItem.status.in_(("pending", "printing")),
            )
            .subquery()
        )
        # At most two rows per printer; do not hydrate the entire queued farm
        # or archive history. Ownership is applied AFTER finding the real head.
        rows = await db.scalars(
            select(PrintQueueItem)
            .join(ranked, ranked.c.id == PrintQueueItem.id)
            .where(ranked.c.rank == 1)
            .options(
                load_only(
                    PrintQueueItem.id,
                    PrintQueueItem.queue_id,
                    PrintQueueItem.status,
                    PrintQueueItem.created_by_id,
                    PrintQueueItem.scheduled_time,
                    PrintQueueItem.manual_start,
                    PrintQueueItem.waiting_reason,
                    PrintQueueItem.waiting_reason_code,
                    PrintQueueItem.waiting_reason_checked_at,
                    # m173: the two columns ``loaded_descriptor`` reads. Deferred
                    # columns are a MissingGreenlet in an async handler, not a
                    # lazy load, so a name that comes out of the snapshot has to
                    # be asked for here.
                    PrintQueueItem.queue_source_id,
                    PrintQueueItem.source_snapshot,
                ),
                selectinload(PrintQueueItem.archive).load_only(PrintArchive.print_name, PrintArchive.filename),
                selectinload(PrintQueueItem.library_file).load_only(LibraryFile.filename),
                selectinload(PrintQueueItem.queue_source),
            )
        )
        heads = {(item.queue_id, item.status): item for item in rows}

    dispatch_state = await background_dispatch.get_state()
    dispatches = {job["printer_id"]: job for job in dispatch_state["active_jobs"]}
    # Queued dispatch jobs already occupy the printer even before an upload.
    for job in dispatch_state["dispatched_jobs"]:
        dispatches.setdefault(job["printer_id"], {"phase": "preparing"})
    out = []
    for printer in printers:
        state, received, stale = printer_manager.peek_status(printer.id)
        active = state is not None and state.state in printer_manager.ACTIVE_PRINT_STATES
        dispatch = dispatches.get(printer.id)
        phase = dispatch.get("phase") if dispatch else None
        tile = MonitorPrinter(
            printer_id=printer.id,
            name=printer.name,
            model=printer.model,
            location=printer.location.path if printer.location else None,
            tags=[tag.name for tag in printer.tags],
            is_active=printer.is_active,
            connected=bool(state and state.connected and not stale),
            state=state.state if state else None,
            status_received_at=datetime.fromtimestamp(received, timezone.utc) if received else None,
            source_stale=stale,
            last_known_work_active=bool(active or dispatch) if received or dispatch else None,
            require_plate_clear=printer.require_plate_clear,
            awaiting_plate_clear=printer_manager.is_awaiting_plate_clear(printer.id) or printer.awaiting_plate_clear,
            dispatch=MonitorDispatch(
                phase=phase if phase in get_args(DispatchPhase) else "preparing",
                upload_progress=dispatch.get("upload_progress_pct"),
            )
            if dispatch
            else None,
        )
        if state is not None:
            tile.progress = state.progress
            tile.remaining_seconds = (
                state.remaining_time * 60 if active and state.remaining_time and state.remaining_time > 0 else None
            )
            tile.layer_num, tile.total_layers = state.layer_num, state.total_layers
            tile.temperatures = display_temperatures(state.temperatures, printer.model)
            # Active HMS already excludes muted entries. The frontend's current
            # filterKnownHMSErrors deliberately retains ALL remaining codes.
            tile.hms_errors = [MonitorHMS(code=e.full_code or e.code, severity=e.severity) for e in state.hms_errors]
            tile.pause_reason = state.pause_reason if state.pause_reason in PAUSE_REASON_LABELS else None
            tile.pause_started_at = (
                datetime.fromtimestamp(state.pause_started_at, timezone.utc) if state.pause_started_at else None
            )
            derived = get_derived_status_name(state, printer.model)
            if state.macro_executing:
                tile.derived_stage = "swapping"
            elif derived in ("Heating heatbed", "Heatbed preheating", "Heating nozzle"):
                tile.derived_stage = "heating"
            elif derived and derived != "Printing" and state.state == "RUNNING":
                tile.derived_stage = "preparing"
            if active:
                running = heads.get((printer.id, "printing"))
                tile.current_job = (
                    _job(running, access)
                    if running
                    else (
                        RestrictedJob()
                        if access.kiosk or not access.read_all
                        else MonitorJob(name=state.subtask_name or state.current_print)
                    )
                )
        queue = queues.get(printer.id)
        if queue:
            head = heads.get((queue.id, "pending"))
            tile.queue = MonitorQueue(
                queue_id=queue.id,
                status=queue.status,
                is_paused=queue.is_paused,
                auto_distribute_eligible=queue.auto_distribute_eligible,
                pending_count=counts.get(queue.id, 0),
                next_job=_job(head, access) if head and view == "queues" else None,
                waiting=_waiting(head, tile, access, now) if view == "queues" else None,
            )
        out.append(tile)
    return MonitorSnapshot(
        generated_at=now,
        view=view,
        printers=out,
        capabilities=MonitorCapabilities(
            queues=access.queue_read,
            forecast=access.queue_read,
            job_details=not access.kiosk,
            open_printer=access.open_printer,
            open_queue=access.open_queue,
        ),
    )
