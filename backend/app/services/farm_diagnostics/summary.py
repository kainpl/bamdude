"""Scoped SQL period aggregates, never an occupancy or operator-performance model.

Only bounded printer groups leave SQL. Full-selection totals are independent of
the printer page. ID ceilings exclude later inserts, not later edits/deletions;
there is deliberately no claim of snapshot isolation across calls.
"""

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import Float, String, and_, case, cast, extract, func, or_, select

from backend.app.mcp_server.contracts import Coverage
from backend.app.models.archive import PrintArchive as Archive
from backend.app.models.printer import Printer
from backend.app.services.farm_diagnostics import paging
from backend.app.services.farm_diagnostics.access import DiagnosticError, printer_scope, require
from backend.app.services.farm_diagnostics.contracts import Evidence, ReadResult
from backend.app.services.queue_sources import current_epoch
from backend.app.services.statistics import outcomes

ENERGY_KINDS = ("measured", "approximate", "estimated", "legacy", "unavailable", "unknown", "missing_legacy")
# SDK publishes both structured data and a text copy. Reserve envelope/cursor
# space under the transport's 128 KiB bound instead of failing a large page.
DETAIL_BYTES = 40 * 1024


def _detail_page(page, grouped):
    details, used = [], 0
    for pid in page:
        row = {"printer_id": pid, **grouped.get(pid, _metrics(None))}
        size = len(json.dumps(row, separators=(",", ":")).encode("utf-8"))
        if used + size > DETAIL_BYTES:
            break
        details.append(row)
        used += size
    return details


def _utc(value):
    return value.astimezone(timezone.utc)


def _valid_number(column):
    # NULL/negative/non-finite stored figures cannot become known consumption.
    return and_(column >= 0, column <= 1e308)


def _facts(db, conditions):
    if db.get_bind().dialect.name == "postgresql":
        seconds = cast(extract("epoch", Archive.completed_at - Archive.started_at), Float)
    else:
        seconds = (func.julianday(Archive.completed_at) - func.julianday(Archive.started_at)) * 86400.0
    endpoints = and_(Archive.started_at.isnot(None), Archive.completed_at.isnot(None))
    positive = and_(endpoints, Archive.completed_at > Archive.started_at)
    synthetic = or_(
        Archive.extra_data["recovered_by_startup_sweep"].as_string().in_(("true", "1")),
        Archive.extra_data["recovered_by_cleanup"].as_string().in_(("true", "1")),
        and_(
            cast(Archive.extra_data["operator_recovery_end"], String).isnot(None),
            cast(Archive.extra_data["operator_recovery_end"], String) != "null",
        ),
    )
    time_kind = case(
        (and_(endpoints, ~positive), "invalid_interval"),
        (and_(positive, ~func.coalesce(synthetic, False)), "measured"),
        (positive, "estimated"),
        (_valid_number(Archive.print_time_seconds), "estimated"),
        else_="missing",
    )
    duration = case((positive, seconds), (_valid_number(Archive.print_time_seconds), Archive.print_time_seconds))
    record = Archive.extra_data["energy_provenance"]
    kind = record["kind"].as_string()
    no_record = or_(cast(record, String).is_(None), cast(record, String).in_(("null", "{}")))
    energy_kind = case(
        (no_record, case((Archive.energy_kwh.is_(None), "missing_legacy"), else_="legacy")),
        (kind == "unavailable", "unavailable"),
        (kind == "estimated", "estimated"),
        (
            and_(kind == "measured", Archive.extra_data["energy_is_approximate"].as_string().in_(("true", "1"))),
            "approximate",
        ),
        (kind == "measured", "measured"),
        else_="unknown",
    )
    known_energy = and_(
        _valid_number(Archive.energy_kwh), energy_kind.in_(("measured", "approximate", "estimated", "legacy"))
    )
    return (
        select(
            Archive.id,
            Archive.printer_id,
            Archive.started_at,
            case((positive, Archive.completed_at)).label("interval_end"),
            case((outcomes.completed(), 1), else_=0).label("completed"),
            case((outcomes.physical_failure(), 1), else_=0).label("physical_failures"),
            case((outcomes.dispatch_failure(), 1), else_=0).label("dispatch_failures"),
            case((outcomes.stopped(), 1), else_=0).label("stopped"),
            time_kind.label("time_kind"),
            duration.label("seconds"),
            energy_kind.label("energy_kind"),
            case((known_energy, Archive.energy_kwh)).label("kwh"),
            case((and_(known_energy, _valid_number(Archive.energy_cost)), Archive.energy_cost)).label("energy_cost"),
            case((_valid_number(Archive.cost), Archive.cost)).label("cost"),
        )
        .where(outcomes.counted(), *conditions)
        .subquery()
    )


def _aggregate(db, conditions, *, grouped=False):
    f = _facts(db, conditions)
    # Compare with the greatest preceding end, not only lag(end): nested
    # intervals otherwise hide overlaps. Selection remains the created_at set,
    # so this is a warning about its intervals, never whole-farm utilization.
    previous_end = func.max(f.c.interval_end).over(
        partition_by=f.c.printer_id, order_by=(f.c.started_at, f.c.id), rows=(None, -1)
    )
    w = select(f, previous_end.label("previous_end")).subquery()
    c = w.c
    metrics = [func.count().label("total")]
    metrics += [
        func.sum(c[name]).label(name) for name in ("completed", "physical_failures", "dispatch_failures", "stopped")
    ]
    for kind in ("measured", "estimated", "missing", "invalid_interval"):
        metrics.append(func.sum(case((c.time_kind == kind, 1), else_=0)).label(kind + "_count"))
        if kind in {"measured", "estimated"}:
            metrics.append(func.sum(case((c.time_kind == kind, c.seconds))).label(kind + "_seconds"))
    metrics.append(
        func.sum(case((and_(c.interval_end.isnot(None), c.started_at < c.previous_end), 1), else_=0)).label(
            "overlapping_interval_count"
        )
    )
    metrics += [
        func.sum(c.kwh).label("kwh"),
        func.count(c.kwh).label("known_energy_count"),
        func.sum(c.energy_cost).label("energy_cost"),
        func.count(c.energy_cost).label("energy_cost_count"),
        func.sum(c.cost).label("cost"),
        func.count(c.cost).label("cost_count"),
    ]
    for kind in ENERGY_KINDS:
        metrics.extend(
            [
                func.sum(case((c.energy_kind == kind, 1), else_=0)).label(kind + "_energy_count"),
                func.count(case((c.energy_kind == kind, c.kwh))).label(kind + "_energy_known_count"),
                func.sum(case((c.energy_kind == kind, c.kwh))).label(kind + "_kwh"),
            ]
        )
    query = select(*metrics)
    return query.add_columns(c.printer_id).group_by(c.printer_id) if grouped else query


def _metrics(row):
    r = dict(row or {})

    def count(name):
        return int(r.get(name) or 0)

    def number(name):
        value = r.get(name)
        return round(float(value), 6) if value is not None else None

    completed, failed, total = count("completed"), count("physical_failures"), count("total")
    return {
        "outcomes": {
            "total": total,
            "completed": completed,
            "physical_failures": failed,
            "dispatch_failures": count("dispatch_failures"),
            "stopped": count("stopped"),
            "other": total - completed - failed - count("dispatch_failures") - count("stopped"),
            "physical_outcome_denominator": completed + failed,
            "success_rate_percent": outcomes.success_rate(completed, failed),
        },
        "time": {
            **{
                k: count(k)
                for k in (
                    "measured_count",
                    "estimated_count",
                    "missing_count",
                    "invalid_interval_count",
                    "overlapping_interval_count",
                )
            },
            "measured_seconds": number("measured_seconds"),
            "estimated_seconds": number("estimated_seconds"),
            "semantics": "Sum of selected archive durations, not clipped occupancy; synthetic ends and slicer fallbacks are estimates. Overlap checks only these selected intervals.",
        },
        "energy": {
            "known_kwh": number("kwh"),
            "known_count": count("known_energy_count"),
            "missing_count": total - count("known_energy_count"),
            "known_energy_cost": number("energy_cost"),
            "cost_known_count": count("energy_cost_count"),
            "by_status": {
                kind: {
                    "count": count(kind + "_energy_count"),
                    "known_count": count(kind + "_energy_known_count"),
                    "kwh": number(kind + "_kwh"),
                }
                for kind in ENERGY_KINDS
            },
        },
        "recorded_cost": {
            "value": number("cost"),
            "known_count": count("cost_count"),
            "missing_count": total - count("cost_count"),
            "basis": "persisted_archive_cost_without_recalculation",
        },
    }


async def period_summary(db, access, args, request_id):
    require(access, "bamdude_get_period_summary")
    started = datetime.now(timezone.utc)
    visible = select(Printer.id).where(printer_scope(access, Printer.id))
    if args.printer_ids is not None:
        requested = sorted(set(args.printer_ids))
        visible = visible.where(Printer.id.in_(requested))
        found = set((await db.scalars(visible)).all())
        if found != set(requested):
            raise DiagnosticError("not_found")
    filters = args.model_dump(mode="json", exclude={"cursor"})
    if args.cursor:
        position = paging.decode_position(args.cursor, access, "period_summary", filters)
        try:
            last, upper, archives = (position[n] for n in ("last", "upper", "archives"))
            assert all(type(v) is int for v in (last, upper, archives)) and 0 < last <= upper and archives >= 0
        except (KeyError, TypeError, AssertionError):
            raise DiagnosticError("invalid_cursor") from None
    else:
        last = 0
        ceilings = (
            await db.execute(
                select(
                    select(func.max(Printer.id)).where(Printer.id.in_(visible)).scalar_subquery(),
                    select(func.max(Archive.id))
                    .where(Archive.printer_id.in_(visible), outcomes.counted())
                    .scalar_subquery(),
                )
            )
        ).one()
        upper, archives = (int(v or 0) for v in ceilings)
    visible = visible.where(Printer.id <= upper)
    page = list((await db.scalars(visible.where(Printer.id > last).order_by(Printer.id).limit(args.limit + 1))).all())
    more = len(page) > args.limit
    page = page[: args.limit]
    conditions = [
        Archive.printer_id.in_(visible),
        Archive.id <= archives,
        Archive.created_at >= _utc(args.from_).replace(tzinfo=None),
        Archive.created_at < _utc(args.to).replace(tzinfo=None),
    ]
    totals = _metrics((await db.execute(_aggregate(db, conditions))).mappings().one())
    rows = (
        (await db.execute(_aggregate(db, [*conditions, Archive.printer_id.in_(page)], grouped=True))).mappings().all()
    )
    grouped = {r["printer_id"]: _metrics(r) for r in rows}
    details = _detail_page(page, grouped)
    more = more or len(details) < len(page)
    page = page[: len(details)]
    if access.epoch != current_epoch():
        raise DiagnosticError("state_changed")
    finished = datetime.now(timezone.utc)
    missing = [
        "state_history_unavailable",
        "orphan_archives_without_visible_printer_excluded",
        "pagination_is_not_snapshot",
        "intervals_outside_created_selection_not_examined",
    ]
    if totals["energy"]["missing_count"]:
        missing.append("print_energy_incomplete")
    if totals["time"]["missing_count"] or totals["time"]["invalid_interval_count"]:
        missing.append("print_duration_incomplete")
    coverage = Coverage(complete=False, missing=missing, truncated=more)
    cursor = (
        paging.encode_position(
            access, "period_summary", filters, {"last": page[-1], "upper": upper, "archives": archives}
        )
        if more
        else None
    )
    zone = ZoneInfo(args.timezone)
    return ReadResult(
        request_id=request_id,
        collection_started_at=started,
        collection_finished_at=finished,
        data={
            "selection_basis": "archive.created_at",
            "pagination_is_snapshot": False,
            "range": {
                "from": _utc(args.from_).isoformat(),
                "to": _utc(args.to).isoformat(),
                "bounds": "[from,to)",
                "timezone": args.timezone,
                "local_from": args.from_.astimezone(zone).isoformat(),
                "local_to": args.to.astimezone(zone).isoformat(),
                "elapsed_seconds": (_utc(args.to) - _utc(args.from_)).total_seconds(),
            },
            "totals_scope": "full_visible_selection_with_id_ceiling_repeated_on_each_page",
            "totals": totals,
            "printers": details,
            "utilization": None,
            "idle_seconds": None,
            "oee": None,
            "recommendations": (
                [
                    {
                        "action_kind": "open_ui",
                        "ui_link": "/archives",
                        "reason": "Review missing print energy and its recorded provenance; missing is not zero.",
                    }
                ]
                if totals["energy"]["missing_count"]
                else []
            ),
        },
        coverage=coverage,
        next_cursor=cursor,
        evidence=[
            Evidence(
                id="period:archives",
                source="scoped_persisted_archives_sql_aggregates",
                collected_at=finished,
                coverage=coverage,
            )
        ],
    )
