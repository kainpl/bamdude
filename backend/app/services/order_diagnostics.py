"""Bounded MCP projections over the existing order readers; no business writes.

UI coverage, checked print output and stock receipts are different facts. The
ordinary greedy attribution supplies the part awards once, including pooled
plates; quality inspection must never redistribute those awards a second time.
"""

import json
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import func, or_, select

from backend.app.mcp_server.contracts import Coverage
from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.app.services import order_fulfilment, order_metrics
from backend.app.services.entity_codes import code_for, id_from_query
from backend.app.services.farm_diagnostics import paging
from backend.app.services.farm_diagnostics.access import DiagnosticError, printer_scope
from backend.app.services.farm_diagnostics.contracts import Evidence, ReadResult
from backend.app.services.order_queue import awaiting_auto_row_conditions, live_archive_conditions


def _order(project):
    return {
        "id": project.id,
        "code": code_for("order", project.id),
        "name": project.name,
        "status": project.status,
        "stage": project.stage,
        "ui_link": f"/projects/{project.id}",
    }


def _result(request_id, started, data, *, missing=(), cursor=None):
    finished = datetime.now(timezone.utc)
    return ReadResult(
        request_id=request_id,
        collection_started_at=started,
        collection_finished_at=finished,
        data=data,
        coverage=Coverage(
            complete=not missing, missing=sorted(set(missing)), truncated=any("truncated" in gap for gap in missing)
        ),
        next_cursor=cursor,
        evidence=[Evidence(id="orders:read", source="persisted_workshop_readers", collected_at=finished)],
        warnings=["names_are_untrusted_data_text", "read_is_not_an_atomic_snapshot"],
    )


async def search_orders(db, access, args, request_id):
    started = datetime.now(timezone.utc)
    filters = {"query": args.query.strip(), "status": args.status}
    query = select(Project)
    if args.status != "any":
        query = query.where(Project.status == args.status)
    if filters["query"]:
        conditions = [Project.name.icontains(filters["query"], autoescape=True)]
        if (order_id := id_from_query("order", filters["query"])) is not None:
            conditions.append(Project.id == order_id)
        query = query.where(or_(*conditions))
    last, upper = 0, await db.scalar(select(func.max(Project.id))) or 0
    if args.cursor:
        last, upper = paging.decode(args.cursor, access, "orders.search", filters)
    rows = list(
        await db.scalars(query.where(Project.id > last, Project.id <= upper).order_by(Project.id).limit(args.limit + 1))
    )
    page = []
    for row in rows[: args.limit]:
        if len(json.dumps([*[_order(p) for p in page], _order(row)]).encode()) > 35000:
            break
        page.append(row)
    cursor = (
        paging.encode(access, "orders.search", filters, last=page[-1].id, upper=upper)
        if len(rows) > len(page)
        else None
    )
    return _result(
        request_id, started, {"orders": [_order(row) for row in page], "pagination_is_snapshot": False}, cursor=cursor
    )


async def order_progress(db, access, args, request_id):
    started = datetime.now(timezone.utc)
    # Bound the normal full-order calculation before hydration. No truncated
    # archive history may masquerade as a complete order's accounting.
    archive_count = await db.scalar(
        select(func.count())
        .select_from(PrintArchive)
        .where(PrintArchive.project_id == args.order_id, *live_archive_conditions())
    )
    line_count = await db.scalar(
        select(func.count()).select_from(ProjectLine).where(ProjectLine.project_id == args.order_id)
    )
    part_count = await db.scalar(
        select(func.count())
        .select_from(PrintArchivePart)
        .join(PrintArchive)
        .where(PrintArchive.project_id == args.order_id, *live_archive_conditions())
    )
    if archive_count > 10000 or line_count > 2000 or part_count > 100000:
        if await db.get(Project, args.order_id) is None:
            raise DiagnosticError("not_found")
        return _result(
            request_id, started, {"order_id": args.order_id, "progress": None}, missing=["order_exceeds_reader_limit"]
        )
    ctx = await order_metrics.load_order_context(db, args.order_id)
    if ctx is None:
        raise DiagnosticError("not_found")
    awards = []
    figures, other = order_metrics.attribute(ctx, part_attributions=awards)
    totals = order_metrics.project_figures(ctx, figures, other)
    fulfilment = await order_fulfilment.state(db, ctx.project, ctx=ctx)
    receipts = {row.line_id: row for row in fulfilment.lines}
    archives = {row.id: row for row in ctx.archives}
    quality = defaultdict(lambda: {"checked_good_from_archives": 0, "unchecked_net_from_archives": 0})
    for award in awards:
        field = (
            "checked_good_from_archives"
            if archives[award.archive_id].outcome_checked_at
            else "unchecked_net_from_archives"
        )
        quality[award.line_id, award.part_id][field] += award.quantity
    missing = []
    if other:
        missing.append("some_order_archives_have_no_counted_line")
    if any(a.status == "completed" and not ctx.archive_parts_by_archive.get(a.id) for a in ctx.archives):
        missing.append("completed_archives_without_part_rows")
    unchecked = sum(a.status == "completed" and a.outcome_checked_at is None for a in ctx.archives)
    missing_checked = any(
        a.status == "completed" and a.outcome_checked_at is not None and not ctx.archive_parts_by_archive.get(a.id)
        for a in ctx.archives
    )
    missing_unchecked = any(
        a.status == "completed" and a.outcome_checked_at is None and not ctx.archive_parts_by_archive.get(a.id)
        for a in ctx.archives
    )
    filters = {"order_id": args.order_id, "line_id": args.line_id, "include_parts": args.include_parts}
    last, upper = 0, max((line.id for line in ctx.lines), default=0)
    if args.cursor:
        last, upper = paging.decode(args.cursor, access, "orders.progress", filters)
    candidates = [
        line for line in ctx.lines if last < line.id <= upper and (args.line_id is None or line.id == args.line_id)
    ]
    if args.line_id is not None and not any(line.id == args.line_id for line in ctx.lines):
        raise DiagnosticError("not_found")
    candidates.sort(key=lambda line: line.id)
    lines = []
    for line in candidates[: args.limit]:
        f, receipt = figures[line.id], receipts[line.id]
        product = ctx.products_by_id.get(line.product_id)
        row = {
            "id": line.id,
            "product_id": line.product_id,
            "product_name": product.name if product else None,
            "unit": "product_units" if f.mode == "product" else "parts",
            "mode": f.mode,
            "ordered": f.quantity,
            "printed_according_to_ui": f.units_printed,
            "covered_according_to_ui": f.covered_units,
            "remaining_according_to_ui": max(0, f.quantity - f.covered_units),
            "from_finished_stock": f.from_finished,
            "from_part_kits": f.from_kit_units,
            "stock_receipts": {
                "received": line.received if f.mode == "product" else None,
                "assembled": line.assembled if f.mode == "product" else None,
                "held": receipt.held,
                "issued": receipt.issued,
                "written_off": receipt.written_off,
            },
            "running_archives": f.prints_in_progress,
            "pending_jobs": f.prints_queued,
            "printed_parts_still_needed": sum(p.remaining for p in f.parts if p.per > 0),
        }
        if args.include_parts:
            row["parts"] = [
                {
                    "id": p.part_id,
                    "name": p.name,
                    "unit": "pieces",
                    "per_unit": p.per,
                    "need_after_stock": p.need,
                    "usable_according_to_ui": p.usable,
                    **quality[line.id, p.part_id],
                    **({"checked_good_from_archives": None} if missing_checked else {}),
                    **({"unchecked_net_from_archives": None} if missing_unchecked else {}),
                    "recount_adjustment": p.count_adjustment,
                    "allocated_from_stock": p.allocated_qty,
                    "stock_used": p.stock_used_qty,
                    "in_progress": p.in_progress,
                    "remaining": p.remaining,
                    "surplus": p.surplus,
                    "stock_receipt": next(
                        (
                            {"held": r.held, "issued": r.issued, "written_off": r.written_off}
                            for r in receipt.parts
                            if r.part_id == p.part_id
                        ),
                        None,
                    ),
                }
                for p in f.parts[:100]
            ]
            if len(f.parts) > 100:
                missing.append("part_detail_truncated_use_order_ui")
            while row["parts"] and len(json.dumps(row).encode()) > 35000:
                row["parts"].pop()
                missing.append("part_detail_truncated_use_order_ui")
        if lines and len(json.dumps([*lines, row]).encode()) > 40000:
            break
        lines.append(row)
    cursor = (
        paging.encode(access, "orders.progress", filters, last=lines[-1]["id"], upper=upper)
        if lines and len(candidates) > len(lines)
        else None
    )
    purchased = order_metrics.procurement_figures(ctx)
    blockers = []
    if any(p.remaining for f in figures.values() for p in f.parts if p.per > 0):
        blockers.append("printed_parts_shortage")
    if any(p.remaining for p in purchased):
        blockers.append("purchased_parts_shortage")
    if not fulfilment.can_complete:
        blockers.append("stock_receipt_or_issue_incomplete")
    else:
        # QC reminders do not add a new completion gate. A received/issued
        # order can close under the Workshop's existing rules even if some
        # historical print outcomes have not been explicitly checked.
        blockers = []
    return _result(
        request_id,
        started,
        {
            "order": _order(ctx.project),
            "lines": lines,
            "summary": {
                "ui_ordered": totals.ordered,
                "ui_covered": totals.covered_units,
                "ui_remaining": totals.remaining,
                "unit": "sum_of_line_units_not_physical_parts",
                "running_archives": totals.prints_in_progress,
                "pending_jobs": totals.prints_queued,
                "unchecked_completed_archives": unchecked,
                "unattributed_archives": len(other),
                "can_complete_by_workshop_rules": fulfilment.can_complete,
            },
            "quality": {
                "warnings": ["unchecked_print_outcomes"] if unchecked else [],
                "checked_completed_archives": sum(
                    a.status == "completed" and a.outcome_checked_at is not None for a in ctx.archives
                ),
                "unchecked_completed_archives": unchecked,
                "recorded_defective_parts": None
                if missing_checked or missing_unchecked
                else sum(p.defective for rows in ctx.archive_parts_by_archive.values() for p in rows),
                "defect_scope": "Recorded per-archive part rows across this order, including unsuccessful attempts; never added to good output.",
            },
            "blockers": blockers,
            "semantics": {
                "ui_counts": "Completed part rows minus recorded defects, with canonical greedy attribution and signed recounts; unchecked outcomes are included by the existing UI.",
                "quality": "Checked/unchecked quantities are the same part awards before recounts, not independent re-attributions. A recount is not a QC verdict. Checked net output is not a stock receipt.",
                "stock_receipts": "Existing line/stock-ledger counters, not additional production. Product and parts lines use different units. Never add receipts to printed output.",
                "remaining": "Existing UI shortage after allocated stock, not yet reduced by future running or queued output. Queue claims are not physical proof.",
                "summary_scope": "Full order totals repeat across line pages. Never add pages' summaries. No unallocated free stock is reserved by this read.",
            },
            "pagination_is_snapshot": False,
        },
        missing=missing,
        cursor=cursor,
    )


async def order_work(db, access, args, request_id):
    started = datetime.now(timezone.utc)
    if args.order_id is not None and await db.get(Project, args.order_id) is None:
        raise DiagnosticError("not_found")
    filters = {"order_id": args.order_id, "state": args.state}
    printing_item = (
        select(PrintQueueItem.id)
        .join(PrinterQueue, PrinterQueue.id == PrintQueueItem.queue_id)
        .where(
            PrintQueueItem.archive_id == PrintArchive.id,
            PrintQueueItem.status == "printing",
            PrinterQueue.printer_id == PrintArchive.printer_id,
            PrintQueueItem.project_id.is_not_distinct_from(PrintArchive.project_id),
        )
        .exists()
    )
    streams = [
        (
            "archive",
            PrintArchive,
            select(PrintArchive).where(
                PrintArchive.status == "printing",
                *live_archive_conditions(),
                printer_scope(access, PrintArchive.printer_id),
                ~printing_item,
            ),
        ),
        (
            "printer_queue",
            PrintQueueItem,
            select(PrintQueueItem)
            .join(PrinterQueue)
            .where(PrintQueueItem.status.in_(["printing", "pending"]), printer_scope(access, PrinterQueue.printer_id)),
        ),
        ("auto_queue", AutoQueueItem, select(AutoQueueItem).where(*awaiting_auto_row_conditions())),
    ]
    if args.state == "running":
        streams[1] = (streams[1][0], streams[1][1], streams[1][2].where(PrintQueueItem.status == "printing"))
    elif args.state == "queued":
        streams[1] = (streams[1][0], streams[1][1], streams[1][2].where(PrintQueueItem.status == "pending"))
    last_kind, last_id, uppers = 0, 0, []
    if args.cursor:
        pos = paging.decode_position(args.cursor, access, "orders.work", filters)
        last_kind, last_id, uppers = pos.get("kind"), pos.get("last"), pos.get("uppers")
        if (
            type(last_kind) is not int
            or not 0 <= last_kind < 3
            or type(last_id) is not int
            or last_id <= 0
            or not isinstance(uppers, list)
            or len(uppers) != 3
            or any(type(n) is not int or n < 0 for n in uppers)
        ):
            raise DiagnosticError("invalid_cursor")
    if not uppers:
        uppers = [await db.scalar(select(func.max(model.id))) or 0 for _, model, _ in streams]
    found = []
    for kind, (source, model, query) in enumerate(streams):
        if (
            kind < last_kind
            or source == "archive"
            and args.state == "queued"
            or source == "auto_queue"
            and (args.state == "running" or access.printer_ids is not None)
        ):
            continue
        if args.order_id is not None:
            query = query.where(model.project_id == args.order_id)
        rows = list(
            await db.scalars(
                query.where(model.id > (last_id if kind == last_kind else 0), model.id <= uppers[kind])
                .order_by(model.id)
                .limit(args.limit + 1)
            )
        )
        found.extend((kind, source, row) for row in rows)
    page = found[: args.limit]
    order_ids = {row.project_id for _, _, row in page if row.project_id is not None}
    orders = (
        {row.id: row for row in await db.scalars(select(Project).where(Project.id.in_(order_ids)))} if order_ids else {}
    )
    line_ids = {row.project_line_id for _, _, row in page if row.project_line_id is not None}
    line_orders = (
        dict(
            (await db.execute(select(ProjectLine.id, ProjectLine.project_id).where(ProjectLine.id.in_(line_ids)))).all()
        )
        if line_ids
        else {}
    )
    queue_ids = {row.queue_id for _, source, row in page if source == "printer_queue"}
    queues = (
        {row.id: row for row in await db.scalars(select(PrinterQueue).where(PrinterQueue.id.in_(queue_ids)))}
        if queue_ids
        else {}
    )
    archive_ids = {
        row.archive_id
        for _, source, row in page
        if source == "printer_queue" and row.status == "printing" and row.archive_id is not None
    }
    execution_archives = (
        {
            row.id: row
            for row in await db.scalars(
                select(PrintArchive).where(
                    PrintArchive.id.in_(archive_ids),
                    PrintArchive.status == "printing",
                    *live_archive_conditions(),
                    printer_scope(access, PrintArchive.printer_id),
                )
            )
        }
        if archive_ids
        else {}
    )
    items = []
    for _kind, source, row in page:
        order = orders.get(row.project_id)
        line_valid = (
            row.project_line_id is not None
            and line_orders.get(row.project_line_id) == row.project_id
            and order is not None
        )
        queue = queues.get(row.queue_id) if source == "printer_queue" else None
        execution = execution_archives.get(row.archive_id) if source == "printer_queue" else None
        execution_id = (
            row.id
            if source == "archive"
            else execution.id
            if execution
            and queue
            and execution.printer_id == queue.printer_id
            and execution.project_id == row.project_id
            else None
        )
        items.append(
            {
                "source": source,
                "id": row.id,
                "execution_archive_id": execution_id,
                "status": row.status,
                "order": _order(order) if order else None,
                "line_id": row.project_line_id if line_valid else None,
                "order_link": "invalid_line_reference"
                if row.project_line_id is not None and not line_valid
                else "explicit_line"
                if line_valid
                else "order_only"
                if order
                else "unassigned",
                "printer_id": queue.printer_id if queue else row.printer_id if source == "archive" else None,
                "queue_paused": queue.is_paused if queue else None,
                "waiting_reason": (getattr(row, "waiting_reason", None) or "")[:300] or None,
                "waiting_reason_code": getattr(row, "waiting_reason_code", None),
                "waiting_reason_checked_at": str(getattr(row, "waiting_reason_checked_at", None))
                if getattr(row, "waiting_reason_checked_at", None)
                else None,
                "target_model": row.target_model if source == "auto_queue" else None,
                "physical_execution_proven": False,
                "ui_link": f"/archives?printer={row.printer_id}"
                if source == "archive"
                else "/queue"
                if source == "auto_queue"
                else f"/queue?printer={queue.printer_id}",
            }
        )
        if len(items) > 1 and len(json.dumps(items).encode()) > 35000:
            items.pop()
            break
    page = page[: len(items)]
    cursor = (
        paging.encode_position(
            access, "orders.work", filters, {"kind": page[-1][0], "last": page[-1][2].id, "uppers": uppers}
        )
        if len(found) > len(page)
        else None
    )
    return _result(
        request_id,
        started,
        {
            "items": items,
            "pagination_is_snapshot": False,
            "semantics": "Pending printer rows plus unassigned pending auto rows; handed-off auto rows are omitted. Running queue rows and orphan archive claims are persisted claims, not proven current printing. Compare item IDs with farm overview/inspect_printer; never infer order identity from filenames.",
        },
        missing=["unassigned_auto_queue_hidden_by_printer_scope"] if access.printer_ids is not None else (),
        cursor=cursor,
    )
