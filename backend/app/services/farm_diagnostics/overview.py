"""SQL scope and keyset page precede monitor hydration and queue aggregation."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import Field
from sqlalchemy import and_, func, select
from sqlalchemy.orm import aliased, load_only, selectinload

from backend.app.mcp_server.contracts import Coverage, DiagnosticAccess, EmptyInput
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.schemas.monitor import MonitorPrinter
from backend.app.services.farm_diagnostics import paging
from backend.app.services.farm_diagnostics.access import DiagnosticError, printer_scope, require
from backend.app.services.farm_diagnostics.contracts import Evidence, Finding, NextStep, PageInput, ReadResult
from backend.app.services.farm_startup_guard import farm_startup_guard
from backend.app.services.monitor_snapshot import MonitorAccess, build_snapshot
from backend.app.services.printer_manager import printer_manager
from backend.app.services.queue_sources import current_epoch


class DiagnosticPrinter(MonitorPrinter):
    connected: bool | None


class QueueClaim(EmptyInput):
    current_item_id: int | None
    header_link_valid: bool | None
    printing_row_count: int


class PrinterFacts(EmptyInput):
    printer: DiagnosticPrinter
    queue_availability: Literal["available", "unavailable_for_actor"]
    observed_at: datetime | None
    freshness: Literal["fresh", "stale", "unknown"]
    coverage: Coverage = Field(default_factory=Coverage)
    claim: QueueClaim | None = None
    data_text_fields: list[str] = Field(
        default_factory=lambda: [
            "printer.name",
            "printer.location",
            "printer.tags",
            "printer.current_job.name",
            "printer.queue.next_job.name",
        ]
    )


class OverviewData(EmptyInput):
    printers: list[PrinterFacts]
    new_work_admission_allowed: bool | None
    selection_basis: Literal["visible_active_catalog_by_id"] = "visible_active_catalog_by_id"
    pagination_is_snapshot: Literal[False] = False


def _live_identity(printer_ids):
    out = []
    for pid in printer_ids:
        state, _, _ = printer_manager.peek_status(pid)
        out.append((pid, state.connection_generation, state.subtask_id, state.current_print) if state else (pid, None))
    return tuple(out)


async def _queue_identity(db, printer_ids):
    # One scalar row per scoped printer, even for corrupt legacy queues. A
    # header pointer never authorizes joining an item from another printer.
    current = aliased(PrintQueueItem)
    counts = (
        select(PrintQueueItem.queue_id, func.count().label("printing_count"))
        .where(
            PrintQueueItem.status == "printing",
            PrintQueueItem.queue_id.in_(select(PrinterQueue.id).where(PrinterQueue.printer_id.in_(printer_ids))),
        )
        .group_by(PrintQueueItem.queue_id)
        .subquery()
    )
    return tuple(
        (
            await db.execute(
                select(
                    PrinterQueue.printer_id,
                    PrinterQueue.current_item_id,
                    current.id.label("linked_item_id"),
                    current.archive_id,
                    current.started_at,
                    func.coalesce(counts.c.printing_count, 0).label("printing_count"),
                )
                .outerjoin(
                    current,
                    and_(current.id == PrinterQueue.current_item_id, current.queue_id == PrinterQueue.id),
                )
                .outerjoin(counts, counts.c.queue_id == PrinterQueue.id)
                .where(PrinterQueue.printer_id.in_(printer_ids))
                .order_by(PrinterQueue.printer_id)
            )
        ).all()
    )


async def read_printers(db, access: DiagnosticAccess, ids: list[int], started: datetime, request_id: str):
    queue_allowed = access.permits("queue:read_all")
    live_before = _live_identity(ids)
    queue_before = await _queue_identity(db, ids) if queue_allowed and ids else None
    printers = (
        list(
            (
                await db.scalars(
                    select(Printer)
                    .where(Printer.id.in_(ids), printer_scope(access, Printer.id), Printer.archived.is_(False))
                    .options(
                        load_only(
                            Printer.id,
                            Printer.name,
                            Printer.model,
                            Printer.is_active,
                            Printer.require_plate_clear,
                            Printer.awaiting_plate_clear,
                        ),
                        selectinload(Printer.location),
                        selectinload(Printer.tags),
                    )
                    .order_by(Printer.id)
                )
            ).all()
        )
        if ids
        else []
    )
    monitor = await build_snapshot(
        db,
        "queues",
        MonitorAccess(
            queue_read=queue_allowed,
            read_all=queue_allowed,
            printer_ids=access.printer_ids,
        ),
        printers=printers,
    )
    if queue_before is not None and await _queue_identity(db, ids) != queue_before:
        raise DiagnosticError("state_changed")
    if access.epoch != current_epoch() or _live_identity(ids) != live_before:
        raise DiagnosticError("state_changed")
    finished = datetime.now(timezone.utc)
    facts, evidence, findings = [], [], []
    claim_rows = {row.printer_id: row for row in queue_before} if queue_before is not None else {}
    for tile in monitor.printers:
        missing = []
        if tile.status_received_at is None:
            missing.append("telemetry_timestamp_unknown")
        elif tile.source_stale:
            missing.append("telemetry_stale")
        freshness = "unknown" if tile.status_received_at is None else "stale" if tile.source_stale else "fresh"
        coverage = Coverage(complete=not missing, missing=missing)
        row = claim_rows.get(tile.printer_id)
        claim = (
            QueueClaim(
                current_item_id=row.linked_item_id,
                header_link_valid=(row.linked_item_id is not None) if row.current_item_id is not None else None,
                printing_row_count=row.printing_count,
            )
            if row is not None
            else None
        )
        diagnostic_printer = DiagnosticPrinter.model_validate(tile.model_dump())
        if freshness != "fresh":
            diagnostic_printer.connected = None
        facts.append(
            PrinterFacts(
                printer=diagnostic_printer,
                queue_availability="available" if queue_allowed else "unavailable_for_actor",
                observed_at=tile.status_received_at,
                freshness=freshness,
                coverage=coverage,
                claim=claim,
            )
        )
        eid = f"printer:{tile.printer_id}"
        evidence.append(
            Evidence(
                id=eid,
                source="monitor_cached_state",
                observed_at=tile.status_received_at,
                collected_at=finished,
                freshness=freshness,
                coverage=coverage,
            )
        )
        queue = tile.queue
        if queue:
            evidence.append(
                Evidence(id=f"queue:{queue.queue_id}", source="persisted_queue_state", collected_at=finished)
            )
        if queue and (
            queue.status == "printing" or claim and (claim.printing_row_count or claim.header_link_valid is not None)
        ):
            busy = bool(tile.dispatch or freshness == "fresh" and tile.state in printer_manager.ACTIVE_PRINT_STATES)
            findings.append(
                Finding(
                    code="printer_busy" if busy else "queue_claim_requires_inspection",
                    severity="info" if busy else "warning",
                    certainty="observed" if busy else "insufficient_evidence",
                    evidence_ids=[eid, f"queue:{queue.queue_id}"],
                    explanation="Queue has a printing claim and current work."
                    if busy
                    else "Queue has a printing claim; this read does not prove it stale or safe to release.",
                    missing_evidence=[] if busy else ["addressed_recovery_classification"],
                    next_steps=[
                        NextStep(
                            action_kind="open_ui",
                            explanation="Inspect the printer and print-state diagnostics.",
                            ui_link=f"/printers?expandPrinter={tile.printer_id}",
                        )
                    ],
                )
            )
    admission_allowed = farm_startup_guard.allowed() if ids else None
    if admission_allowed is False:
        evidence.append(Evidence(id="farm:admission", source="farm_startup_guard", collected_at=finished))
        findings.append(
            Finding(
                code="farm_admission_held",
                severity="warning",
                certainty="observed",
                evidence_ids=["farm:admission"],
                explanation="Installation admission currently blocks new work; current prints may continue.",
                next_steps=[
                    NextStep(
                        action_kind="open_ui",
                        explanation="Review the installation's startup pause.",
                        ui_link="/printers",
                    )
                ],
            )
        )
    return ReadResult(
        request_id=request_id,
        collection_started_at=started,
        collection_finished_at=finished,
        data=OverviewData(printers=facts, new_work_admission_allowed=admission_allowed).model_dump(mode="json"),
        evidence=evidence,
        findings=findings,
        coverage=Coverage(
            complete=all(item.coverage.complete for item in facts),
            missing=sorted({value for item in facts for value in item.coverage.missing}),
        ),
        warnings=["names_are_untrusted_data_text", "waiting_reason_time_is_not_telemetry_heartbeat"],
    )


async def overview(db, access: DiagnosticAccess, args: PageInput, request_id: str) -> ReadResult:
    require(access, "bamdude_get_farm_overview")
    started = datetime.now(timezone.utc)
    filters = {"limit": args.limit}
    predicate = (Printer.archived.is_(False), printer_scope(access, Printer.id))
    if args.cursor:
        last, upper = paging.decode(args.cursor, access, "overview", filters)
    else:
        last, upper = 0, (await db.scalar(select(func.max(Printer.id)).where(*predicate))) or 0
    ids = list(
        (
            await db.scalars(
                select(Printer.id)
                .where(*predicate, Printer.id > last, Printer.id <= upper)
                .order_by(Printer.id)
                .limit(args.limit + 1)
            )
        ).all()
    )
    more = len(ids) > args.limit
    ids = ids[: args.limit]
    result = await read_printers(db, access, ids, started, request_id)
    if more:
        result.next_cursor = paging.encode(access, "overview", filters, last=ids[-1], upper=upper)
        result.coverage.complete = False
        result.coverage.truncated = True
        result.coverage.missing.append("additional_pages_available")
    return result
