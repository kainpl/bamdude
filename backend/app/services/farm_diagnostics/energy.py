"""Bounded persisted energy evidence; never poll, assess, integrate or backfill.

Snapshot tables have no historical source identity. Only the current binding's
strict source cutoff admits their rows. Archive-owned figures remain independent
of that binding, with page totals separate from observed counter windows.
"""

import hashlib
import json
import math
from datetime import datetime, timezone

from sqlalchemy import and_, case, func, or_, select, tuple_

from backend.app.mcp_server.contracts import Coverage
from backend.app.models.archive import PrintArchive as Archive
from backend.app.models.printer import Printer
from backend.app.models.smart_plug import SmartPlug as Plug
from backend.app.models.smart_plug_energy_snapshot import SmartPlugEnergySnapshot as Snapshot
from backend.app.models.smart_plug_meter_incident import SmartPlugMeterIncident as Incident
from backend.app.models.smart_plug_power_history import SmartPlugPowerHistory as Power
from backend.app.services.farm_diagnostics import paging
from backend.app.services.farm_diagnostics.access import DiagnosticError, printer_scope, require
from backend.app.services.farm_diagnostics.contracts import Evidence, Finding, NextStep, ReadResult
from backend.app.services.meter_watch import energy_wire_fields
from backend.app.services.queue_sources import current_epoch

MAX_PLUGS = 100
UNAVAILABLE_REASONS = {
    "source_changed",
    "unknown_end",
    "insufficient_budget",
    "insufficient_history",
    "missing_start",
    "counter_reset",
    "no_energy",
    "no_total",
    "start_missing",
    "storage_replaced",
    "clock_before_source",
}
QUALITY_REASONS = {"state_unknown", "invalid_input", "batch_unknown", "buffer_loss"}
INTEGRAL_REASONS = {"no_samples", "coverage", "edge_head", "edge_tail"}


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def naive(value):
    return utc(value).replace(tzinfo=None)


def number(value):
    return float(value) if type(value) in (float, int) and math.isfinite(value) and value >= 0 else None


def date(value):
    try:
        return utc(datetime.fromisoformat(value)).isoformat() if isinstance(value, str) else None
    except ValueError:
        return None


async def _bindings(db, pid):
    return list(
        (
            await db.execute(
                select(Plug.id, Plug.plug_type, Plug.measurement_source_id, Plug.measurement_source_since)
                .where(Plug.printer_id == pid)
                .order_by(Plug.id)
                .limit(MAX_PLUGS + 1)
            )
        ).all()
    )


def _fingerprint(rows):
    return hashlib.sha256(
        json.dumps(
            [
                [
                    r.id,
                    r.plug_type,
                    r.measurement_source_id,
                    utc(r.measurement_source_since).isoformat() if r.measurement_source_since else None,
                ]
                for r in rows
            ],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _source_scope(rows, model, time_column, *, incident=False):
    return or_(
        False,
        *[
            and_(
                model.plug_id == r.id,
                time_column > naive(r.measurement_source_since),
                *([model.source_id == r.measurement_source_id] if incident else []),
            )
            for r in rows
            if r.measurement_source_id and r.measurement_source_since is not None
        ],
    )


def _archive_fact(row):
    record = row.provenance if isinstance(row.provenance, dict) else {}
    extra = {"energy_provenance": record, "energy_is_approximate": row.approximate is True}
    fields = energy_wire_fields(extra, row.energy_kwh)
    missing = []
    reason = fields["energy_reason"]
    if reason is not None and (not isinstance(reason, str) or reason not in UNAVAILABLE_REASONS | {"meter_stalled"}):
        reason = "unknown_recorded_reason"
        missing.append(reason)
    if not record:
        status = "missing_legacy" if row.energy_kwh is None else "legacy"
        missing.append("historical_provenance_missing")
    elif not isinstance(record.get("kind"), str) or record["kind"] not in {"measured", "estimated", "unavailable"}:
        status = "unknown"
        missing.append("unknown_energy_kind")
    else:
        status = fields["energy_status"] or "unknown"
    value = number(row.energy_kwh)
    if value is None:
        missing.append("print_energy_unknown")
    if status in {"unavailable", "unknown"}:
        value = None  # do not promote an inconsistent/unknown persisted record to a measurement
    basis = {"kind": status, "start": date(record.get("start")), "end": date(record.get("end"))}
    # Numeric quality metadata only. Unknown keys and arbitrary reason strings
    # from JSON never escape, even if a future writer starts recording secrets.
    for name in (
        "counter_delta_kwh",
        "coverage",
        "max_gap_s",
        "samples",
        "integral_kwh",
        "gap_limit_s",
        "expected_interval_s",
    ):
        v = number(record.get(name))
        if v is not None:
            basis[name] = v
    quality = record.get("quality_reasons")
    if isinstance(quality, list):
        basis["quality_reasons"] = sorted(
            {v if isinstance(v, str) and v in INTEGRAL_REASONS else "unknown_recorded_reason" for v in quality}
        )
    coverage = number(fields["energy_coverage"])
    return {
        "archive_id": row.id,
        "created_at": utc(row.created_at).isoformat(),
        "status": row.status
        if row.status in {"completed", "failed", "cancelled", "printing", "pending"}
        else "unknown",
        "energy_status": status,
        "energy_reason": reason,
        "energy_coverage": coverage,
        "energy_kwh": value,
        "value_recorded": row.energy_kwh is not None,
        "energy_cost": number(row.energy_cost) if value is not None else None,
        "start_counter_kwh": number(row.energy_start_kwh),
        "provenance": basis,
        "coverage": Coverage(complete=not missing, missing=missing).model_dump(),
    }


def _print_summary(facts):
    buckets = dict.fromkeys(("measured", "approximate", "estimated", "legacy"), 0.0)
    for fact in facts:
        if fact["energy_status"] in buckets and fact["energy_kwh"] is not None:
            buckets[fact["energy_status"]] += fact["energy_kwh"]
    return {
        "scope": "this_page",
        "returned_count": len(facts),
        **{f"{kind}_print_kwh": round(value, 6) for kind, value in buckets.items()},
        "known_print_kwh": round(sum(buckets.values()), 6),
        "null_count": sum(not f["value_recorded"] for f in facts),
        "unusable_count": sum(f["energy_kwh"] is None for f in facts),
        "zero_count": sum(f["energy_kwh"] == 0 for f in facts),
        "estimate_count": sum(f["energy_status"] == "estimated" for f in facts),
        "missing_legacy_count": sum(f["energy_status"] == "missing_legacy" for f in facts),
        "unavailable_count": sum(f["energy_status"] == "unavailable" for f in facts),
        "unknown_count": sum(f["energy_status"] == "unknown" for f in facts),
        "start_counter_count": sum(f["start_counter_kwh"] is not None for f in facts),
    }


async def inspect_energy(db, access, args, request_id):
    require(access, "bamdude_inspect_energy")
    started = datetime.now(timezone.utc)
    pid = await db.scalar(
        select(Printer.id)
        .where(Printer.id == args.printer_id, Printer.archived.is_(False), printer_scope(access, Printer.id))
        .limit(1)
    )
    if pid is None:
        raise DiagnosticError("not_found")
    bindings = await _bindings(db, pid)
    source_hash = _fingerprint(bindings)
    plugs = bindings[:MAX_PLUGS]
    lo, hi = naive(args.from_), naive(args.to)
    filters = args.model_dump(mode="json", by_alias=True, exclude={"cursor"})
    conditions = {
        "archives": [
            Archive.printer_id == pid,
            printer_scope(access, Archive.printer_id),
            Archive.deleted_at.is_(None),
            Archive.created_at >= lo,
            Archive.created_at < hi,
        ],
        "snapshots": [
            _source_scope(plugs, Snapshot, Snapshot.recorded_at),
            Snapshot.recorded_at >= lo,
            Snapshot.recorded_at < hi,
        ],
        "incidents": [
            _source_scope(plugs, Incident, Incident.opened_at, incident=True),
            Incident.flat_since < hi,
            or_(Incident.closed_at.is_(None), Incident.closed_at >= lo),
        ],
    }
    models = {
        "archives": (Archive, Archive.created_at),
        "snapshots": (Snapshot, Snapshot.recorded_at),
        "incidents": (Incident, Incident.opened_at),
    }
    if args.cursor:
        state = paging.decode_position(args.cursor, access, "energy", filters)
        try:
            bounds, last = state["bounds"], state["last"]
            assert all(type(bounds[n]) is int and bounds[n] >= 0 for n in models)
            assert all(
                last[n] is None or len(last[n]) == 2 and type(last[n][1]) is int and last[n][1] > 0 for n in models
            )
            for n in models:
                if last[n]:
                    datetime.fromisoformat(last[n][0])
        except (KeyError, TypeError, ValueError, AssertionError, IndexError):
            raise DiagnosticError("invalid_cursor") from None
        if state.get("sources") != source_hash:
            raise DiagnosticError("state_changed")
    else:
        # One SQL statement, three scoped scalar subqueries; no whole-table dump.
        # A preceding baseline may have a larger ID than the in-range samples
        # when rows were inserted out of time order.
        upper_conditions = {
            **conditions,
            "snapshots": [_source_scope(plugs, Snapshot, Snapshot.recorded_at), Snapshot.recorded_at < hi],
        }
        upper = (
            await db.execute(
                select(
                    *[
                        select(func.max(model.id)).where(*upper_conditions[name]).scalar_subquery().label(name)
                        for name, (model, _) in models.items()
                    ]
                )
            )
        ).one()
        bounds = {name: getattr(upper, name) or 0 for name in models}
        last = dict.fromkeys(models)
    queries = {
        "archives": select(
            Archive.id,
            Archive.created_at,
            Archive.status,
            Archive.energy_kwh,
            Archive.energy_cost,
            Archive.energy_start_kwh,
            Archive.extra_data["energy_provenance"].label("provenance"),
            Archive.extra_data["energy_is_approximate"].as_boolean().label("approximate"),
        ),
        "snapshots": select(Snapshot.id, Snapshot.plug_id, Snapshot.recorded_at, Snapshot.lifetime_kwh),
        "incidents": select(
            Incident.id,
            Incident.plug_id,
            Incident.opened_at,
            Incident.flat_since,
            Incident.flat_until,
            Incident.closed_at,
            Incident.status,
            Incident.close_reason,
        ),
    }
    pages, more = {}, {}
    for name, query in queries.items():
        model, at = models[name]
        clauses = [*conditions[name], model.id <= bounds[name]]
        if last[name]:
            clauses.append(tuple_(at, model.id) > (datetime.fromisoformat(last[name][0]), last[name][1]))
        rows = list((await db.execute(query.where(*clauses).order_by(at, model.id).limit(args.limit + 1))).all())
        pages[name], more[name] = rows[: args.limit], len(rows) > args.limit

    # At most one preceding sample per current plug, in one window query.
    # It is not a zero-filled baseline; the source cutoff applies here too.
    preceding = (
        tuple_(Snapshot.recorded_at, Snapshot.id)
        <= (datetime.fromisoformat(last["snapshots"][0]), last["snapshots"][1])
        if last["snapshots"]
        else Snapshot.recorded_at <= lo
    )
    ranked = (
        select(
            Snapshot.id,
            Snapshot.plug_id,
            Snapshot.recorded_at,
            Snapshot.lifetime_kwh,
            func.row_number()
            .over(partition_by=Snapshot.plug_id, order_by=(Snapshot.recorded_at.desc(), Snapshot.id.desc()))
            .label("rn"),
        )
        .where(_source_scope(plugs, Snapshot, Snapshot.recorded_at), preceding, Snapshot.id <= bounds["snapshots"])
        .subquery()
    )
    baselines = {
        r.plug_id: r for r in (await db.execute(select(ranked).where(ranked.c.rn == 1).limit(MAX_PLUGS))).all()
    }
    quality = case(
        (Power.unusable_reason.in_(sorted(QUALITY_REASONS)), Power.unusable_reason), else_="unknown_recorded_reason"
    )
    quality_rows = (
        await db.execute(
            select(Power.plug_id, quality.label("reason"), func.count().label("count"))
            .where(
                _source_scope(plugs, Power, Power.recorded_at),
                Power.recorded_at >= lo,
                Power.recorded_at < hi,
                Power.unusable_reason.is_not(None),
            )
            .group_by(Power.plug_id, quality)
            .limit(MAX_PLUGS * (len(QUALITY_REASONS) + 1))
        )
    ).all()

    facts = [_archive_fact(r) for r in pages["archives"]]
    plug_facts = []
    for plug in plugs:
        missing = []
        if not plug.measurement_source_id or plug.measurement_source_since is None:
            missing.append("source_cutoff_unknown")
        elif naive(plug.measurement_source_since) >= lo:
            missing.append("range_precedes_current_source")
        baseline = baselines.get(plug.id)
        samples = [r for r in pages["snapshots"] if r.plug_id == plug.id]
        previous = baseline
        decreases = []
        growth, compared = 0.0, False
        for sample in samples:
            before, after = number(previous.lifetime_kwh) if previous else None, number(sample.lifetime_kwh)
            if before is not None and after is not None and previous.id != sample.id:
                compared = True
                if after < before:
                    decreases.append(
                        {"at": utc(sample.recorded_at).isoformat(), "before_kwh": before, "after_kwh": after}
                    )
                else:
                    growth += after - before
            elif after is None:
                missing.append("invalid_counter_sample")
            previous = sample
        if baseline is None:
            missing.append("counter_baseline_missing")
        if not samples:
            missing.append("no_counter_samples_on_page")
        if decreases:
            missing.append("counter_decreased_reset_or_source_unknown")
        # Even a comparable delta describes sampled endpoints, not an exact
        # requested interval or the energy of the archives selected by creation.
        missing.append("counter_edges_are_sampled_not_exact_range")
        if more["snapshots"]:
            missing.append("counter_page_truncated")
        incidents = [
            {
                "id": r.id,
                "meaning": "persisted_meter_suspicion",
                "opened_at": utc(r.opened_at).isoformat(),
                "flat_since": utc(r.flat_since).isoformat(),
                "flat_until": utc(r.flat_until).isoformat(),
                "closed_at": utc(r.closed_at).isoformat() if r.closed_at else None,
                "status": r.status if r.status in {"active", "closed"} else "unknown",
                "close_reason": r.close_reason
                if r.close_reason in {None, "recovered", "source_changed", "removed"}
                else "unknown_recorded_reason",
            }
            for r in pages["incidents"]
            if r.plug_id == plug.id
        ]
        quality_counts = {r.reason: r.count for r in quality_rows if r.plug_id == plug.id}
        if quality_counts:
            missing.append("power_history_has_quality_gaps")
        plug_facts.append(
            {
                "plug_id": plug.id,
                "provider": plug.plug_type
                if plug.plug_type in {"tasmota", "homeassistant", "mqtt", "rest", "zigbee"}
                else "unknown",
                "basis": "currently_bound_plug_after_source_cutoff",
                "units": "kWh",
                "source_since": utc(plug.measurement_source_since).isoformat()
                if plug.measurement_source_since
                else None,
                "baseline": {"at": utc(baseline.recorded_at).isoformat(), "lifetime_kwh": number(baseline.lifetime_kwh)}
                if baseline
                else None,
                "snapshots": [
                    {"id": r.id, "at": utc(r.recorded_at).isoformat(), "lifetime_kwh": number(r.lifetime_kwh)}
                    for r in samples
                ],
                "counter_delta_kwh": round(growth, 6)
                if compared and baseline is not None and not decreases and "invalid_counter_sample" not in missing
                else None,
                "counter_delta_scope": "this_page_sample_window",
                "range_counter_kwh": None,
                "observed_growth_kwh": round(growth, 6) if compared else None,
                "decreases": decreases,
                "incidents": incidents,
                "quality_counts": quality_counts,
                "quality_selection_basis": "whole_requested_range_after_current_source_cutoff",
                "coverage": Coverage(
                    complete=not missing, missing=missing, truncated=more["snapshots"] or more["incidents"]
                ).model_dump(),
            }
        )
    if access.epoch != current_epoch() or _fingerprint(await _bindings(db, pid)) != source_hash:
        raise DiagnosticError("state_changed")
    truncated = any(more.values()) or len(bindings) > MAX_PLUGS
    next_cursor = None
    if any(more.values()):
        next_last = dict(last)
        for name, rows in pages.items():
            if rows:
                at = getattr(
                    rows[-1],
                    "created_at" if name == "archives" else "recorded_at" if name == "snapshots" else "opened_at",
                )
                next_last[name] = [naive(at).isoformat(), rows[-1].id]
        next_cursor = paging.encode_position(
            access, "energy", filters, {"bounds": bounds, "last": next_last, "sources": source_hash}
        )
    finished = datetime.now(timezone.utc)
    missing = ["historical_binding_not_reconstructed", "counter_and_print_windows_differ"]
    if truncated:
        missing.append("partial_page")
    if len(bindings) > MAX_PLUGS:
        missing.append("plug_list_truncated")
    print_missing = sorted({m for fact in facts for m in fact["coverage"]["missing"]})
    evidence = [
        Evidence(
            id="energy:prints",
            source="persisted_archive_energy_by_created_at",
            collected_at=finished,
            coverage=Coverage(
                complete=not print_missing and not more["archives"], missing=print_missing, truncated=more["archives"]
            ),
        )
    ]
    evidence.append(
        Evidence(
            id="energy:counters",
            source="current_source_persisted_counter_samples",
            collected_at=finished,
            coverage=Coverage(complete=False, missing=missing, truncated=truncated),
        )
    )
    findings = []
    if any(f["status"] == "completed" and f["energy_kwh"] is None for f in facts) and any(
        (p["observed_growth_kwh"] or 0) > 0 for p in plug_facts
    ):
        findings.append(
            Finding(
                code="energy_counter_grows_print_accounting_missing",
                severity="warning",
                certainty="observed",
                evidence_ids=["energy:prints", "energy:counters"],
                explanation="Comparable counter samples grew while visible completed archives on this page lack usable energy. This is coincidence, not attribution of that delta to those prints.",
                missing_evidence=["print_consumption_not_proven_by_plug_delta"],
                next_steps=[
                    NextStep(
                        action_kind="open_ui",
                        explanation="Review the persisted print energy and its provenance.",
                        ui_link="/archives",
                    )
                ],
            )
        )
    return ReadResult(
        request_id=request_id,
        collection_started_at=started,
        collection_finished_at=finished,
        data={
            "printer_id": pid,
            "selection_basis": "archive.created_at",
            "pagination_is_snapshot": False,
            "range": {
                "from": utc(args.from_).isoformat(),
                "to": utc(args.to).isoformat(),
                "bounds": "[from,to)",
                "exact_counter_coverage": False,
            },
            "prints": facts,
            "print_summary": _print_summary(facts),
            "plugs": plug_facts,
            "page_sections_truncated": more,
            "counter_semantics": "Observed current-plug sample windows include idle usage; they are not the sum of print energy.",
        },
        coverage=Coverage(complete=False, missing=missing, truncated=truncated),
        next_cursor=next_cursor,
        evidence=evidence,
        findings=findings,
    )
