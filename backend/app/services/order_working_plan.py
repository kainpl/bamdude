"""One shared working draft and an operator-confirmed comparison baseline.

All changes serialize on the draft row. These guards never acquire a source
or printer lane; publication takes them only after its existing guards.
"""

import hmac
import secrets
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from backend.app.core.database import take_write_lock
from backend.app.core.db_dialect import insert_ignoring_conflicts
from backend.app.models.order_working_plan import OrderWorkingPlan
from backend.app.schemas.order_draft import DraftEntry
from backend.app.schemas.order_working_plan import WorkingEntry
from backend.app.services import order_draft as draft, order_plan_access as access_policy
from backend.app.services.plan_engine import LinePlan, PlanRow, plan_totals

# A proposal is a reviewed payload, not a promise to recompute a new fleet split.
# A process restart invalidates proposals safely; the editor must review again.
_proposal_key = secrets.token_bytes(32)


def proposal_signature(project_id, request, revision, inputs_token, entries, actor):
    body = draft.digest([project_id, request.kind, revision, inputs_token, entries, actor])
    return hmac.digest(_proposal_key, body.encode(), "sha256").hex()


def refusal(code="plan_conflict"):
    return HTTPException(409, {"code": code, "message": "The working plan changed. Read it again."})


async def lock(db, project_id, *, create=False):
    if create:
        await db.execute(insert_ignoring_conflicts(db, OrderWorkingPlan, [{"id": project_id}], ["id"]))
    await take_write_lock(db, OrderWorkingPlan.__table__, project_id)
    return await db.scalar(
        select(OrderWorkingPlan)
        .where(OrderWorkingPlan.id == project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def check(row, revision, *, frozen=True):
    if row is None or row.revision != revision:
        raise refusal()
    if frozen and row.active_operation:
        raise refusal("submission_pending")


def parse(entries):
    return [WorkingEntry.model_validate(e) for e in entries]


def executable(entries):
    # WorkingEntry has already validated counts including explicit zero exclusions.
    return [
        DraftEntry.model_construct(
            entry_id=e.entry_id,
            line_id=e.line_id,
            line_token=e.line_token,
            plate_id=e.plate_id,
            count=e.count,
            target=e.target,
            source_token=e.source_token,
        )
        for e in parse(entries)
    ]


def projection(data, entries, facts, *, visible=None):
    valid_lines = {line.id for line in data.ctx.lines}
    regular = [e for e in executable(entries) if e.line_id in valid_lines]
    plan, coverage, issues, _ = draft.project(data, regular, visible=visible)
    issues.extend(
        {"entry_id": e["entry_id"], "code": "line_unavailable"} for e in entries if e["line_id"] not in valid_lines
    )
    by_entry = {e["entry_id"]: e for e in entries}
    # Attach identities by the original per-line order, never by file name.
    bad = {i["entry_id"] for i in issues}
    for line in plan.lines:
        line_entries = [e for e in entries if e["line_id"] == line.line_id and e["entry_id"] not in bad]
        for row, entry in zip(line.rows, line_entries, strict=True):
            row.entry_id = entry["entry_id"]
            row.line_token = entry.get("line_token")
            row.source_token = entry.get("source_token")
            row.manual = entry.get("manual", False)
            row.protected_count = entry.get("protected_count", 0)
    for issue in issues:
        entry = by_entry[issue["entry_id"]]
        line = next((line for line in plan.lines if line.line_id == entry["line_id"]), None)
        if line is None:
            line = LinePlan(line_id=entry["line_id"], product_id=0, material=None)
            plan.lines.append(line)
        prior = facts.get("entries", {}).get(entry["entry_id"], {})
        line.rows.append(
            PlanRow(
                plate_id=entry["plate_id"],
                library_file_id=prior.get("file_id", 0),
                plate_index=prior.get("plate_index", 0),
                filename="",
                count=entry["count"],
                time_unknown=True,
                target_printer_id=entry.get("target", {}).get("printer_id"),
                entry_id=entry["entry_id"],
                line_token=entry.get("line_token"),
                source_token=entry.get("source_token"),
                manual=entry.get("manual", False),
                protected_count=entry.get("protected_count", 0),
                issue=issue["code"],
            )
        )
    plan.totals = plan_totals(plan.lines, data.price)
    return plan, coverage, issues


def normalize(data, incoming, previous, visible):
    previous = {e["entry_id"]: e for e in previous}
    candidates = {plate.id: (plate, file, recipe) for rows in data.recipes.values() for plate, file, recipe in rows}
    current_lines = {line.id for line in data.ctx.lines}
    products = {line.id: line.product_id for line in data.ctx.lines}
    line_tokens = {line.id: line.plan_identity for line in data.ctx.lines}
    entries = []
    for item in incoming:
        entry = item.model_dump()
        old = previous.get(item.entry_id)
        retained = old is not None and (old["line_id"], old["plate_id"]) == (item.line_id, item.plate_id)
        candidate = candidates.get(item.plate_id)
        if item.line_id not in current_lines and not retained:
            raise HTTPException(422, {"code": "invalid_draft"})
        if not retained and (candidate is None or not visible(candidate[1])):
            raise HTTPException(403, {"code": "source_unavailable"})
        if not retained and candidate[0].product_id != products.get(item.line_id):
            raise HTTPException(422, {"code": "invalid_draft"})
        if item.source_token is None:
            entry["source_token"] = old.get("source_token") if retained else data.source_tokens.get(item.plate_id)
        if item.line_token is None:
            entry["line_token"] = old.get("line_token") if retained else line_tokens.get(item.line_id)
        entries.append(entry)
    # project also verifies line/product membership, including retained sources.
    projection(data, entries, {}, visible=visible)
    return entries


def snapshot(data, entries, plan, coverage, issues):
    rows = {row.entry_id: row for line in plan.lines for row in line.rows}
    return {
        "inputs_token": data.token,
        "coverage": coverage,
        "workload": draft.workload(plan, executable(entries), issues),
        "entries": {
            e["entry_id"]: {
                "file_id": rows[e["entry_id"]].library_file_id,
                "plate_index": rows[e["entry_id"]].plate_index,
                "source_token": e.get("source_token"),
                "seconds": rows[e["entry_id"]].print_time_seconds,
                "yield": {str(part): count for part, count in rows[e["entry_id"]].useful.items()},
            }
            for e in entries
            if e["entry_id"] in rows
        },
    }


def response(row, data, *, visible=None):
    entries = deepcopy(row.entries) if row else []
    _, _, issues = projection(data, entries, row.facts if row else {}, visible=visible)
    for issue in issues:
        entry = next(e for e in entries if e["entry_id"] == issue["entry_id"])
        if issue["code"] == "source_changed":
            issue["current_source_token"] = data.source_tokens.get(entry["plate_id"])
    return {
        "schema_version": 1,
        "revision": row.revision if row else 0,
        "entries": entries,
        "saved_revision": row.saved_revision if row else 0,
        "saved_at": row.saved_at.replace(tzinfo=timezone.utc).isoformat() if row and row.saved_at else None,
        "saved_by": row.saved_by if row else None,
        "epoch": row.epoch if row else 0,
        "saved_epoch": row.saved_epoch if row else None,
        "active_operation": row.active_operation if row else None,
        "inputs_token": data.token,
        "issues": issues,
        "changed": bool(row and row.saved_entries != row.entries),
        "inputs_changed": bool(row and row.facts.get("inputs_token") != data.token),
        "saved_entries": deepcopy(row.saved_entries) if row else None,
        "saved_machine_seconds": (row.saved_facts or {}).get("workload", {}).get("total_seconds") if row else None,
    }


async def effective_plans(db, plans, contexts, recipes, queued, price):
    """Batch overlay: callers already loaded every order/recipe once."""
    from backend.app.services.order_metrics import attribute
    from backend.app.services.plan_engine import PlanAlternative, line_yield
    from backend.app.services.product_composition import estimate_seconds

    stored = {
        row.id: row for row in (await db.scalars(select(OrderWorkingPlan).where(OrderWorkingPlan.id.in_(plans)))).all()
    }
    for ctx in contexts:
        data = draft.from_context(ctx, recipes, queued, price, baseline=plans[ctx.project.id])
        plans[ctx.project.id].inputs_token = data.token
        line_tokens = {line.id: line.plan_identity for line in ctx.lines}
        for line in plans[ctx.project.id].lines:
            for entry in line.rows:
                entry.line_token = line_tokens.get(line.line_id)
                entry.source_token = data.source_tokens.get(entry.plate_id)
                for alternative in entry.alternatives:
                    alternative.source_token = data.source_tokens.get(alternative.plate_id)
        row = stored.get(ctx.project.id)
        if row is None or ctx.project.status != "active":
            continue
        plan, _, _ = projection(data, row.entries, row.facts)
        plan.inputs_token = data.token
        figures = attribute(ctx)[0]
        for line in plan.lines:
            used = {r.plate_id for r in line.rows}
            counted = (
                {p.part_id for p in figures[line.line_id].parts if p.per > 0 and p.kind == "printed"}
                if line.line_id in figures
                else set()
            )
            for entry in line.rows:
                if entry.issue:
                    continue
                entry.alternatives = [
                    PlanAlternative(
                        plate_id=plate.id,
                        library_file_id=file.id,
                        plate_index=plate.plate_index,
                        filename="",
                        printer_model=recipe.printer_model,
                        print_time_seconds=estimate_seconds(recipe),
                        filament_used_grams=recipe.filament_used_grams,
                        cost=recipe.filament_used_grams * price
                        if recipe.filament_used_grams is not None and price is not None
                        else None,
                        time_unknown=estimate_seconds(recipe) is None,
                        source_token=data.source_tokens[plate.id],
                    )
                    for plate, file, recipe in recipes.get(line.product_id, [])
                    if plate.id not in used and recipe.sliced and line_yield(recipe, counted) == entry.useful
                ]
        plan.mode = "working"
        plan.working_revision = row.revision
        plans[ctx.project.id] = plan


async def read(db, project_id, visible):
    data = await draft.inputs(db, project_id)
    row = await db.get(OrderWorkingPlan, project_id, populate_existing=True)
    return await state(db, row, data, visible=visible)


async def state(db, row, data, *, visible=None):
    out = response(row, data, visible=visible)
    pinned = [e for e in out["entries"] if e.get("target", {}).get("kind") == "printer"]
    if pinned:
        from backend.app.models.printer import Printer
        from backend.app.utils.model_compatibility import model_compatibility

        printers = {
            p.id: p
            for p in (
                await db.scalars(select(Printer).where(Printer.id.in_({e["target"]["printer_id"] for e in pinned})))
            ).all()
        }
        recipes = {plate.id: recipe for candidates in data.recipes.values() for plate, _, recipe in candidates}
        for entry in pinned:
            target = printers.get(entry["target"]["printer_id"])
            recipe = recipes.get(entry["plate_id"])
            if target is None or target.archived:
                out["issues"].append({"entry_id": entry["entry_id"], "code": "target_unavailable"})
            else:
                from backend.app.services.order_resources import permits

                if entry["count"] > 0 and not permits(
                    getattr(data.ctx.project, "printer_resources", None), target.id, target.model
                ):
                    out["issues"].append({"entry_id": entry["entry_id"], "code": "order_resources_changed"})
            if target and recipe and model_compatibility(recipe.printer_model, target.model) == "incompatible":
                out["issues"].append({"entry_id": entry["entry_id"], "code": "target_incompatible"})
    return out


async def comparison(
    db, data, plan, entries, coverage, issues, *, working_row, visible=None, saved_revision=None, epoch=None
):
    """Prepare nominal comparison before live farm reads; never move its base."""
    row = working_row
    if saved_revision is not None and saved_revision != (row.saved_revision if row else 0):
        return {"status": "baseline_changed"}, None
    if row is None or row.saved_entries is None:
        return {"status": "no_baseline"}, None
    if row.saved_epoch != row.epoch or epoch is not None and epoch != row.epoch:
        return {"status": "comparison_scope_changed", "saved_revision": row.saved_revision}, None
    previous, previous_coverage, previous_issues = projection(
        data, row.saved_entries, row.saved_facts or {}, visible=visible
    )
    if previous_issues or issues:
        return {"status": "source_changed", "saved_revision": row.saved_revision}, None
    previous_seconds = (row.saved_facts or {}).get("workload", {}).get("total_seconds")
    current_seconds = draft.workload(plan, entries, issues)["total_seconds"]
    if previous_seconds is None or current_seconds is None:
        return {"status": "incomplete", "saved_revision": row.saved_revision}, None

    def counts(rows, key):
        return sorted((p["line_id"], p["part_id"], p[key]) for p in rows)

    changed = counts(coverage, "planned") != counts(previous_coverage, "planned") or counts(
        coverage, "required"
    ) != counts((row.saved_facts or {}).get("coverage", []), "required")
    out = {
        "status": "ok",
        "saved_revision": row.saved_revision,
        "machine_seconds_delta": current_seconds - previous_seconds,
        "coverage_changed": changed,
        "now_seconds_delta": None,
        "after_seconds_delta": None,
    }
    comparable_date = not changed and not any(p["missing"] for p in previous_coverage)
    return out, previous if comparable_date else None


async def write(db, project_id, request, access):
    # Validate existence before an FK insert. All authority/inputs are read again
    # after the draft guard, on this connection.
    await draft.inputs(db, project_id)
    row = await lock(db, project_id, create=request.expected_revision == 0)
    if request.expected_revision == 0:
        if row is None or row.revision != 1 or row.entries or row.facts:
            raise refusal()
    else:
        check(row, request.expected_revision)
    user, scope, _ = await access_policy.fresh(db, access)

    def visible(file):
        return access_policy.visible(file, user, scope)

    data = await draft.inputs(db, project_id)
    if data.ctx.project.status != "active":
        raise refusal("order_closed")
    if request.inputs_token and request.inputs_token != data.token:
        raise refusal("inputs_changed")
    entries = normalize(data, request.entries, row.entries, visible)
    plan, coverage, issues = projection(data, entries, row.facts, visible=visible)
    if entries != row.entries or not row.facts:
        row.entries = entries
        row.facts = snapshot(data, entries, plan, coverage, issues)
        row.updated_by = access.actor.label
        if request.expected_revision:
            row.revision += 1
    await db.flush()
    return await state(db, row, data, visible=visible)


async def act(db, project_id, request, access, *, discard=False):
    row = await lock(db, project_id)
    check(row, request.expected_revision)
    if row.saved_revision != request.expected_saved_revision or row.epoch != request.expected_epoch:
        raise refusal()
    user, scope, _ = await access_policy.fresh(db, access)

    def visible(file):
        return access_policy.visible(file, user, scope)

    data = await draft.inputs(db, project_id)
    if data.ctx.project.status != "active":
        raise refusal("order_closed")
    if request.inputs_token and request.inputs_token != data.token:
        raise refusal("inputs_changed")
    if discard:
        if row.saved_entries is None or row.saved_epoch != row.epoch:
            raise refusal("comparison_scope_changed")
        row.entries = deepcopy(row.saved_entries)
        row.updated_by = access.actor.label
        row.revision += 1
    else:
        plan, coverage, issues = projection(data, row.entries, row.facts, visible=visible)
        facts = snapshot(data, row.entries, plan, coverage, issues)
        row.facts = facts
        if row.saved_entries != row.entries or row.saved_epoch != row.epoch or row.saved_facts != facts:
            row.saved_entries = deepcopy(row.entries)
            row.saved_facts = facts
            row.saved_epoch = row.epoch
            row.saved_revision += 1
            row.saved_at = datetime.now(timezone.utc).replace(tzinfo=None)
            row.saved_by = access.actor.label
    await db.flush()
    return await state(db, row, data, visible=visible)


async def replan(db, project_id, request, access):
    from backend.app.services.order_metrics import attribute
    from backend.app.services.plan_engine import (
        _current_fleet_capacity,
        counted_parts_by_line,
        plan_lines,
        queued_yield_by_line,
    )

    row = (
        await lock(db, project_id)
        if request.apply
        else await db.get(OrderWorkingPlan, project_id, populate_existing=True)
    )
    check(row, request.expected_revision)
    user, scope, _ = await access_policy.fresh(db, access)

    def visible(file):
        return access_policy.visible(file, user, scope)

    data = await draft.inputs(db, project_id)
    if data.ctx.project.status != "active":
        raise refusal("order_closed")
    if request.inputs_token and request.inputs_token != data.token:
        raise refusal("inputs_changed")
    if request.apply:
        if request.proposal_entries is None:
            raise refusal("inputs_changed")
        proposed = [e.model_dump(mode="json") for e in request.proposal_entries]
    else:
        manual = (
            []
            if request.kind == "rebuild"
            else [
                {**entry, "count": entry.get("protected_count", entry["count"])}
                for entry in row.entries
                if entry.get("manual")
            ]
        )
        manual_plan, _, issues = projection(data, manual, row.facts, visible=visible)
        if any(next(e["count"] for e in manual if e["entry_id"] == issue["entry_id"]) > 0 for issue in issues):
            raise refusal("source_review_required")
        figures = attribute(data.ctx)[0]
        queued = await queued_yield_by_line(
            db, data.recipes, data.ctx.lines, counted_parts_by_line({project_id: figures})
        )
        for line in manual_plan.lines:
            for entry in line.rows:
                for part_id, count in entry.useful.items():
                    queued.setdefault(line.line_id, {})[part_id] = (
                        queued.get(line.line_id, {}).get(part_id, 0) + count * entry.count
                    )
        excluded = {(e["line_id"], e["plate_id"]) for e in manual if e["count"] == 0}
        fleet = await _current_fleet_capacity(db)
        proposed = deepcopy(manual)
        for line in data.ctx.lines:
            recipes = {
                line.product_id: [
                    candidate
                    for candidate in data.recipes.get(line.product_id, [])
                    if visible(candidate[1]) and (line.id, candidate[0].id) not in excluded
                ]
            }
            auto = plan_lines(replace(data.ctx, lines=[line]), figures, recipes, queued, data.price, fleet)
            for section in auto.lines:
                for selected in section.rows:
                    match = next(
                        (e for e in proposed if e["line_id"] == line.id and e["plate_id"] == selected.plate_id),
                        None,
                    )
                    if match:
                        match["count"] += selected.count
                    else:
                        proposed.append(
                            {
                                "entry_id": f"auto-{line.id}-{selected.plate_id}",
                                "line_id": line.id,
                                "line_token": line.plan_identity,
                                "plate_id": selected.plate_id,
                                "count": selected.count,
                                "manual": False,
                                "protected_count": 0,
                                "target": {"kind": "auto"},
                                "source_token": data.source_tokens[selected.plate_id],
                            }
                        )
    from backend.app.schemas.order_working_plan import WorkingWrite

    # Apply the same aggregate and per-entry caps, including auto + manual.
    try:
        validated = WorkingWrite(expected_revision=row.revision, entries=proposed)
    except ValidationError as exc:
        raise HTTPException(422, {"code": "plan_budget_exceeded"}) from exc
    proposed = [e.model_dump(mode="json") for e in validated.entries]
    proposal = proposal_signature(project_id, request, row.revision, data.token, proposed, access.identity)
    if not request.apply:
        old_state = await state(db, row, data, visible=visible)
        bad = {issue["entry_id"] for issue in old_state["issues"]}
        proposed_identities = {draft.entry_identity(entry) for entry in proposed}
        return {
            "proposal_token": proposal,
            "inputs_token": data.token,
            "entries": proposed,
            "previous_entries": deepcopy(row.entries),
            "previous_count": sum(e["count"] for e in row.entries),
            "count": sum(e["count"] for e in proposed),
            "removed_issue_entry_count": sum(
                e["entry_id"] in bad and draft.entry_identity(e) not in proposed_identities for e in row.entries
            ),
        }
    if not request.proposal_token or not hmac.compare_digest(proposal, request.proposal_token):
        raise refusal("inputs_changed")
    entries = normalize(data, validated.entries, row.entries, visible)
    plan, coverage, issues = projection(data, entries, row.facts, visible=visible)
    if entries != row.entries:
        row.entries = entries
        row.updated_by = access.actor.label
        row.facts = snapshot(data, entries, plan, coverage, issues)
        row.revision += 1
    await db.flush()
    return await state(db, row, data, visible=visible)
