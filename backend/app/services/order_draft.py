"""Read-only exact draft projection shared by preview and durable working plans.

No file reads, capture or printer commands belong here. A semantic token names
planning inputs, never changing farm occupancy or a display label.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime

from fastapi import HTTPException

from backend.app.schemas.order_draft import DraftEntry
from backend.app.services import farm_forecast as ff
from backend.app.services.filament_cost import default_rate_per_kg
from backend.app.services.order_metrics import attribute, batch_contexts, line_accepts_materials
from backend.app.services.plan_engine import (
    OrderPlan,
    PlanRow,
    counted_parts_by_line,
    line_yield,
    plan_for_orders,
    plan_lines,
    plan_totals,
    queued_yield_by_line,
)
from backend.app.services.product_composition import estimate_seconds, recipes_for_products

_simulation_slots = asyncio.Semaphore(2)
logger = logging.getLogger(__name__)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


@dataclass
class Inputs:
    ctx: object
    baseline: OrderPlan
    recipes: dict
    token: str
    price: float | None
    source_tokens: dict[int, str]


async def inputs(db, project_id: int) -> Inputs:
    contexts = await batch_contexts(db, [project_id])
    if not contexts:
        raise HTTPException(404, "Project not found")
    ctx = contexts[0]
    recipes = await recipes_for_products(db, ctx.products_by_id.values())
    figures = attribute(ctx)[0]
    queued = await queued_yield_by_line(db, recipes, ctx.lines, counted_parts_by_line({project_id: figures}))
    rate = await default_rate_per_kg(db)
    price = rate / 1000 if rate > 0 else None
    return from_context(ctx, recipes, queued, price)


def from_context(ctx, recipes, queued, price, *, baseline=None):
    recipes = {product_id: recipes.get(product_id, []) for product_id in ctx.products_by_id}
    figures = attribute(ctx)[0]
    baseline = baseline if baseline is not None else plan_lines(ctx, figures, recipes, queued, price)
    source_tokens = {
        plate.id: digest(
            [
                plate.id,
                file.id,
                file.file_hash,
                file.file_size,
                file.fs_modified_at,
                plate.plate_index,
                asdict(recipe) | {"materials": sorted(recipe.materials), "colors": sorted(recipe.colors)},
            ]
        )
        for rows in recipes.values()
        for plate, file, recipe in rows
    }
    token = digest(
        [
            ctx.project.status,
            ctx.project.order_print_options,
            getattr(ctx.project, "printer_resources", None),
            ctx.project.auto_eject_enabled,
            {line.id: line.plan_identity for line in ctx.lines},
            [
                (
                    line.line_id,
                    line.product_id,
                    line.material,
                    line.outstanding_before,
                    [(p.part_id, p.per, p.need, p.usable, p.in_progress) for p in figures[line.line_id].parts],
                )
                for line in baseline.lines
            ],
            source_tokens,
            price,
        ]
    )
    return Inputs(ctx, baseline, recipes, token, price, source_tokens)


def entry_identity(entry: dict) -> tuple:
    return tuple(entry.get(key) for key in ("entry_id", "line_id", "plate_id", "line_token", "source_token"))


async def preview_projection(db, data: Inputs, entries: list[DraftEntry], *, visible=None):
    """Only a currently retained identity can explain an absent order line.

    Keep its unknown work in issues, never as a synthetic simulation row.
    The same saved snapshot is also the forecast comparison's authority.
    """
    from backend.app.models.order_working_plan import OrderWorkingPlan

    stored = await db.get(OrderWorkingPlan, data.ctx.project.id, populate_existing=True)
    identities = {entry_identity(e) for e in stored.entries} if stored else set()
    lines = {line.id for line in data.ctx.lines}
    regular, missing = [], []
    for entry in entries:
        if entry.line_id in lines:
            regular.append(entry)
        elif entry_identity(entry.model_dump()) in identities:
            missing.append({"entry_id": entry.entry_id, "code": "line_unavailable"})
        else:
            raise HTTPException(422, {"code": "invalid_draft", "message": "Order line does not belong to this order"})
    plan, coverage, issues, targets = project(data, regular, visible=visible)
    return plan, coverage, issues + missing, targets, stored


def project(data: Inputs, entries: list[DraftEntry], *, visible=None):
    lines = {line.line_id: replace(line, rows=[]) for line in data.baseline.lines}
    requested = {line.id: line for line in data.ctx.lines}
    issues = []
    targets = {}
    figures = attribute(data.ctx)[0]
    made = {line_id: Counter() for line_id in lines}
    for entry in entries:
        line = requested.get(entry.line_id)
        if line is None:
            raise HTTPException(422, {"code": "invalid_draft", "message": "Order line does not belong to this order"})
        if entry.line_token is not None and entry.line_token != line.plan_identity:
            issues.append({"entry_id": entry.entry_id, "code": "line_unavailable"})
            continue
        candidate = next((c for c in data.recipes.get(line.product_id, []) if c[0].id == entry.plate_id), None)
        if candidate is None or (visible and not visible(candidate[1])):
            issues.append({"entry_id": entry.entry_id, "code": "source_unavailable"})
            continue
        plate, file, recipe = candidate
        if entry.source_token is not None and entry.source_token != data.source_tokens.get(plate.id):
            issues.append({"entry_id": entry.entry_id, "code": "source_changed"})
            continue
        if not recipe.sliced or not line_accepts_materials(line, recipe.materials):
            issues.append({"entry_id": entry.entry_id, "code": "source_incompatible"})
            continue
        from backend.app.services.order_resources import recipe_permitted

        # A retained zero is an explicit exclusion during remainder planning,
        # not a request to run this model. Keep identity/source checks above.
        if entry.count > 0 and not recipe_permitted(
            getattr(data.ctx.project, "printer_resources", None), recipe.printer_model, allow_compatible=True
        ):
            issues.append({"entry_id": entry.entry_id, "code": "order_resources_changed"})
            continue
        counted = {p.part_id for p in figures[line.id].parts if p.per > 0 and p.kind == "printed"}
        yield_ = line_yield(recipe, counted)
        if not yield_:
            issues.append({"entry_id": entry.entry_id, "code": "source_incompatible"})
            continue
        made[line.id].update({pid: n * entry.count for pid, n in yield_.items()})
        grams = recipe.filament_used_grams
        lines[line.id].rows.append(
            PlanRow(
                plate_id=plate.id,
                library_file_id=file.id,
                plate_index=plate.plate_index,
                filename="",
                count=entry.count,
                useful=yield_,
                print_time_seconds=estimate_seconds(recipe),
                filament_used_grams=grams,
                cost=grams * data.price if grams is not None and data.price is not None else None,
                time_unknown=estimate_seconds(recipe) is None,
                printer_model=recipe.printer_model,
                target_printer_id=entry.target.printer_id,
            )
        )
        if entry.target.kind == "printer":
            targets[(entry.line_id, entry.plate_id)] = entry.target.printer_id
    coverage = []
    for line in lines.values():
        for pid in sorted(set(line.outstanding_before) | set(made[line.line_id])):
            need = line.outstanding_before.get(pid, 0)
            produced = made[line.line_id][pid]
            coverage.append(
                {
                    "line_id": line.line_id,
                    "part_id": pid,
                    "required": need,
                    "planned": produced,
                    "missing": max(0, need - produced),
                    "surplus": max(0, produced - need),
                    "need": next((p.need for p in figures[line.line_id].parts if p.part_id == pid), 0),
                    "covered": max(
                        0, next((p.need for p in figures[line.line_id].parts if p.part_id == pid), 0) - need
                    ),
                }
            )
        line.surplus_after = {
            pid: n - line.outstanding_before.get(pid, 0)
            for pid, n in made[line.line_id].items()
            if n > line.outstanding_before.get(pid, 0)
        }
        line.unsatisfiable = []
        line.unsatisfiable_reasons = {}
    plan = replace(data.baseline, lines=list(lines.values()), truncated=False)
    plan.unplanned_parts = {
        line.line_id: sum(p["missing"] for p in coverage if p["line_id"] == line.line_id) for line in plan.lines
    }
    plan.totals = plan_totals(plan.lines, data.price)
    return plan, coverage, issues, targets


def wire(f):
    out = asdict(f)
    out["incomplete_reasons"] = [{"code": c, "count": n} for c, n in f.incomplete_reasons]
    return out


def workload(plan, entries, issues):
    """Nominal slicer time never depends on a printer's availability."""

    def group(rows):
        known = sum(r.count * r.print_time_seconds for r in rows if r.print_time_seconds is not None)
        unknown = sum(r.count for r in rows if r.print_time_seconds is None)
        return {
            "known_seconds": known,
            "unknown_prints": unknown,
            "total_seconds": None if unknown else known,
            "prints": sum(r.count for r in rows),
        }

    rows = [r for line in plan.lines for r in line.rows]
    out = group(rows)
    bad_ids = {i["entry_id"] for i in issues}
    represented = {row.entry_id for row in rows if row.entry_id}
    missing = sum(e.count for e in entries if e.entry_id in bad_ids and e.entry_id not in represented)
    if missing:
        out.update(unknown_prints=out["unknown_prints"] + missing, total_seconds=None, prints=out["prints"] + missing)
    models = sorted({r.printer_model for r in rows}, key=lambda m: m or "")
    out["by_model"] = [{"model": m, **group([r for r in rows if r.printer_model == m])} for m in models]
    out["by_line"] = []
    for line in plan.lines:
        counts = group(line.rows)
        missing_line = sum(
            e.count
            for e in entries
            if e.line_id == line.line_id and e.entry_id in bad_ids and e.entry_id not in represented
        )
        if missing_line:
            counts.update(
                total_seconds=None,
                unknown_prints=counts["unknown_prints"] + missing_line,
                prints=counts["prints"] + missing_line,
            )
        out["by_line"].append({"line_id": line.line_id, **counts})
    if missing:
        unknown_model = next((m for m in out["by_model"] if m["model"] is None), None)
        if unknown_model is None:
            unknown_model = {"model": None, **group([])}
            out["by_model"].append(unknown_model)
        unknown_model.update(
            total_seconds=None,
            unknown_prints=unknown_model["unknown_prints"] + missing,
            prints=unknown_model["prints"] + missing,
        )
    return out


async def simulate(snapshot, plans, walk, project_id, now):
    # The semaphore stays owned until the real thread ends, including cancellation.
    async with _simulation_slots:
        task = asyncio.create_task(asyncio.to_thread(ff.forecast_orders, snapshot, plans, walk, {project_id}, now))
        try:
            return (await asyncio.shield(task))[project_id]
        except asyncio.CancelledError:
            from backend.app.services.preview_artifacts import owned

            await owned(task)
            raise


async def preview(
    db,
    project_id,
    entries,
    now: datetime,
    *,
    inputs_token=None,
    visible=None,
    printer_scope=None,
    saved_revision=None,
    comparison_epoch=None,
):
    data = await inputs(db, project_id)
    if inputs_token and inputs_token != data.token:
        raise HTTPException(
            409, {"code": "inputs_changed", "message": "Planning inputs changed", "inputs_token": data.token}
        )
    plan, coverage, issues, _, stored = await preview_projection(db, data, entries, visible=visible)
    complete = not issues and not any(p["missing"] for p in coverage)
    answer = {
        "schema_version": 1,
        "inputs_token": data.token,
        "forecast": None,
        "coverage": coverage,
        "coverage_complete": complete,
        "issues": issues,
        "order_ready_at": None,
        "totals": asdict(plan.totals) | {"rows": sum(len(line.rows) for line in plan.lines)},
        "workload": workload(plan, entries, issues),
    }
    from backend.app.services.order_working_plan import comparison

    answer["comparison"], previous = await comparison(
        db,
        data,
        plan,
        entries,
        coverage,
        issues,
        visible=visible,
        saved_revision=saved_revision,
        epoch=comparison_epoch,
        working_row=stored,
    )
    try:
        snapshot = await ff.load_snapshot(db, now)
        if printer_scope is not None:
            snapshot.printers = [p for p in snapshot.printers if p.printer_id in printer_scope]
        ranked = await ff.rank_active_orders(db)
        walk = ranked[: ranked.index(project_id) + 1] if project_id in ranked else [project_id]
        plans = await plan_for_orders(db, walk, capacity=ff.capacity_from_snapshot(snapshot))
        plans[project_id] = plan
        f = await simulate(snapshot, plans, walk, project_id, now)
        accepting = ff.accepting_models(snapshot, project_id)
        f.by_model = ff.by_model(plan, accepting)
        f.machine_seconds = answer["workload"]["total_seconds"]
        f.late = complete and ff.eta_is_late(f.now_eta if f.eta_complete else None, data.ctx.project.due_date)
        if issues:
            f.eta_complete = False
        answer.update(forecast=wire(f), order_ready_at=f.now_eta if complete and f.eta_complete else None)
        if previous is not None and complete and f.eta_complete:
            baseline_plans = {**plans, project_id: previous}
            old = await simulate(snapshot, baseline_plans, walk, project_id, now)
            if old.eta_complete:
                for key in ("now_seconds", "after_seconds"):
                    before, after = getattr(old, key), getattr(f, key)
                    if before is not None and after is not None:
                        answer["comparison"][key + "_delta"] = after - before
    except Exception as exc:
        logger.warning("Draft forecast unavailable: %s", type(exc).__name__)
        await db.rollback()
        answer["forecast_error"] = "forecast_unavailable"
    return answer
