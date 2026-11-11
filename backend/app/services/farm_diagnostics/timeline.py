"""Bounded archive-owned history. Names and current status never correlate runs."""

from datetime import datetime, timezone

from sqlalchemy import String, cast, func, literal, select, tuple_, union_all

from backend.app.mcp_server.contracts import Coverage
from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.print_state_recovery_action import PrintStateRecoveryAction
from backend.app.models.print_usage_event import PrintUsageEvent
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services.farm_diagnostics import paging
from backend.app.services.farm_diagnostics.access import DiagnosticError, printer_scope, require
from backend.app.services.farm_diagnostics.contracts import Evidence, ReadResult
from backend.app.services.print_usage_journal import without_stale_refill_runouts
from backend.app.services.queue_sources import current_epoch
from backend.app.utils.usage_attribution import UNKNOWN_FEED

HISTORY_BUDGET = 2000
USAGE_EVENTS = {"start", "tray_change", "tail_start", "feed_return", "runout", "spool_loaded", "pause", "resume"}
USAGE_KINDS = {"pause", "autoswitch", "external", "ambiguous", "manual", "gap"}
RECOVERY_ACTIONS = {
    "align_terminal_item",
    "fill_estimated_end_and_align",
    "release_orphan_header",
    "delete_stale_item",
    "close_interrupted_attempt",
    "clear_terminal_runtime_context",
    "cancel_terminal_claims",
    "settle_swap_by_operator",
    "restore_gate_owner_row",
}


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


async def timeline(db, access, args, request_id):
    require(access, "bamdude_get_print_timeline")
    started = datetime.now(timezone.utc)
    a, q, u, r, s = PrintArchive, PrintQueueItem, PrintUsageEvent, PrintStateRecoveryAction, SpoolUsageHistory
    target = (
        await db.execute(
            select(
                a.id,
                a.printer_id,
                a.status,
                a.created_at,
                a.started_at,
                a.completed_at,
                a.print_time_seconds,
                a.extra_data["operator_recovery_end"]["source"].as_string().label("end_source"),
            ).where(a.id == args.archive_id, a.deleted_at.is_(None), printer_scope(access, a.printer_id))
        )
    ).first()
    if target is None:
        raise DiagnosticError("not_found")
    aid, pid = target.id, target.printer_id
    filters = args.model_dump(mode="json", by_alias=True, exclude={"cursor"})
    if args.cursor:
        position = paging.decode_position(args.cursor, access, "timeline", filters)
        try:
            bounds, last = position["bounds"], position["last"]
            ceiling = datetime.fromisoformat(position["ceiling"])
            last = (datetime.fromisoformat(last[0]), last[1], last[2], last[3])
            assert all(type(bounds[k]) is int and bounds[k] >= 0 for k in ("usage", "queue", "spool"))
        except (KeyError, TypeError, ValueError, AssertionError, IndexError):
            raise DiagnosticError("invalid_cursor") from None
    else:
        bounds = {}
        for name, model in (("usage", u), ("queue", q), ("spool", s)):
            if (
                name == "queue"
                and not access.permits("queue:read_all")
                or name == "spool"
                and not access.permits("inventory:read")
            ):
                bounds[name] = 0
                continue
            relation = (
                model.queue_id.in_(select(PrinterQueue.id).where(PrinterQueue.printer_id == pid))
                if name == "queue"
                else model.printer_id == pid
            )
            bounds[name] = await db.scalar(select(func.max(model.id)).where(model.archive_id == aid, relation)) or 0
        recovery_upper = (
            (
                await db.execute(
                    select(r.created_at, r.id)
                    .where(r.archive_id == aid, r.printer_id == pid)
                    .order_by(r.created_at.desc(), r.id.desc())
                    .limit(1)
                )
            ).first()
            if access.permits("queue:read_all")
            else None
        )
        bounds["recovery"] = [recovery_upper.created_at.isoformat(), recovery_upper.id] if recovery_upper else None
        ceiling, last = started.replace(tzinfo=None), None

    # Retention cannot prove the absence of historical events. Read bounded ancestry
    # BEFORE from/page so the shared stale-refill rule sees the same prior feed.
    missing = ["usage_journal_retention_unknown", "queue_transition_history_not_recorded"]
    history = list(
        (
            await db.scalars(
                select(u)
                .where(
                    u.archive_id == aid,
                    u.printer_id == pid,
                    u.id <= bounds["usage"],
                )
                .order_by(u.id)
                .limit(HISTORY_BUDGET + 1)
            )
        ).all()
    )
    if len(history) > HISTORY_BUDGET:
        accepted = []
        missing.append("usage_ancestry_budget_exceeded")
    else:
        accepted = without_stale_refill_runouts(history)
    usage_by_id = {str(row.id): row for row in accepted}

    def event(model, at, source, *predicates):
        sequence = literal(0) if model is r else model.id
        return select(
            at.label("at"),
            literal(source).label("source"),
            sequence.label("sequence"),
            cast(model.id, String).label("id"),
        ).where(
            model.archive_id == aid,
            *predicates,
            at.is_not(None),
            at <= ceiling,
        )

    streams = []
    for field, value in (("created", target.created_at), ("start", target.started_at), ("end", target.completed_at)):
        if value is not None:
            streams.append(
                select(
                    literal(value).label("at"),
                    literal("archive." + field).label("source"),
                    literal(aid).label("sequence"),
                    literal(str(aid)).label("id"),
                )
            )
    if accepted:
        streams.append(event(u, u.created_at, "usage", u.printer_id == pid, u.id.in_([row.id for row in accepted])))
    if access.permits("queue:read_all"):
        for field in ("created_at", "started_at", "completed_at"):
            streams.append(
                event(
                    q,
                    getattr(q, field),
                    "queue." + field,
                    q.id <= bounds["queue"],
                    q.queue_id.in_(select(PrinterQueue.id).where(PrinterQueue.printer_id == pid)),
                )
            )
        if bounds["recovery"]:
            streams.append(
                event(
                    r,
                    r.created_at,
                    "recovery",
                    r.printer_id == pid,
                    tuple_(r.created_at, r.id)
                    <= (datetime.fromisoformat(bounds["recovery"][0]), bounds["recovery"][1]),
                )
            )
    else:
        missing.append("queue_evidence_unavailable_for_actor")
    if access.permits("inventory:read"):
        streams.append(event(s, s.created_at, "spool", s.id <= bounds["spool"], s.printer_id == pid))
    else:
        missing.append("inventory_identity_and_amounts_unavailable_for_actor")
    if not streams:
        rows = []
    else:
        events = union_all(*streams).subquery()
        statement = select(events).where(events.c.at <= ceiling)
        if args.from_:
            statement = statement.where(events.c.at >= args.from_.astimezone(timezone.utc).replace(tzinfo=None))
        if args.to:
            statement = statement.where(events.c.at < args.to.astimezone(timezone.utc).replace(tzinfo=None))
        if last:
            statement = statement.where(tuple_(events.c.at, events.c.source, events.c.sequence, events.c.id) > last)
        rows = (
            await db.execute(
                statement.order_by(events.c.at, events.c.source, events.c.sequence, events.c.id).limit(args.limit + 1)
            )
        ).all()
    more, rows = len(rows) > args.limit, rows[: args.limit]
    recovery_ids = [row.id for row in rows if row.source == "recovery"]
    recoveries = (
        dict(
            (
                await db.execute(
                    select(r.id, r.action).where(r.id.in_(recovery_ids), r.archive_id == aid, r.printer_id == pid)
                )
            ).all()
        )
        if recovery_ids
        else {}
    )
    spool_ids = [int(row.id) for row in rows if row.source == "spool"]
    spools = (
        {
            str(row.id): row
            for row in (
                await db.execute(
                    select(s.id, s.spool_id, s.weight_used, s.status).where(
                        s.id.in_(spool_ids), s.archive_id == aid, s.printer_id == pid
                    )
                )
            ).all()
        }
        if spool_ids
        else {}
    )
    items = []
    for row in rows:
        item = {
            "evidence_id": f"{row.source}:{row.id}",
            "source": row.source,
            "source_id": row.id,
            "observed_at": utc(row.at).isoformat(),
            "basis": "persisted_timestamp",
        }
        if row.source == "archive.end" and target.end_source == "slicer_estimate":
            item["basis"] = "slicer_estimate"
        if row.source == "usage":
            raw = usage_by_id[row.id]
            item.update(
                event=raw.event if raw.event in USAGE_EVENTS else "unknown",
                kind=raw.kind if raw.kind in USAGE_KINDS else None,
                layer=raw.layer_num,
                tray=raw.global_tray_id,
                slot_occupied=raw.slot_occupied,
                feed_unknown=raw.global_tray_id is None or raw.global_tray_id == UNKNOWN_FEED,
            )
            if access.permits("inventory:read"):
                item.update(spool_id=raw.spool_id, spoolman_spool_id=raw.spoolman_spool_id)
        elif row.source == "spool":
            raw = spools[row.id]
            item.update(
                spool_id=raw.spool_id,
                grams=raw.weight_used,
                record_kind="lifetime_correction" if raw.status in {"runout", "ams_sync"} else "print_usage",
            )
        elif row.source == "recovery":
            action = recoveries[row.id]
            item["action"] = action if action in RECOVERY_ACTIONS else "unknown"
        items.append(item)
    final_target = (
        await db.execute(select(a.printer_id, a.status, a.started_at, a.completed_at, a.deleted_at).where(a.id == aid))
    ).first()
    if (
        access.epoch != current_epoch()
        or final_target is None
        or tuple(final_target) != (pid, target.status, target.started_at, target.completed_at, None)
    ):
        raise DiagnosticError("state_changed")
    token = None
    if more:
        end = rows[-1]
        token = paging.encode_position(
            access,
            "timeline",
            filters,
            {
                "bounds": bounds,
                "ceiling": ceiling.isoformat(),
                "last": [end.at.isoformat(), end.source, end.sequence, end.id],
            },
        )
        missing.append("additional_pages_available")
    finished = datetime.now(timezone.utc)
    return ReadResult(
        request_id=request_id,
        collection_started_at=started,
        collection_finished_at=finished,
        data={
            "archive_id": aid,
            "printer_id": pid,
            "current_status": target.status
            if target.status in {"printing", "completed", "failed", "cancelled"}
            else "unknown",
            "slicer_estimate_seconds": target.print_time_seconds,
            "events": items,
            "selection_basis": "persisted_event_time",
            "pagination_is_atomic_snapshot": False,
        },
        evidence=[
            Evidence(
                id=item["evidence_id"], source=item["source"], observed_at=item["observed_at"], collected_at=finished
            )
            for item in items
        ],
        coverage=Coverage(complete=False, missing=missing, truncated=more),
        next_cursor=token,
        warnings=["current_status_is_not_transition_history", "absent_history_does_not_prove_absence_of_events"],
    )
