"""One order's allowed printers, shared by planning and existing queue admission.

This is a filter, not a scheduler or reservation. Already claimed work keeps
its lifecycle. Changing the filter does not rewrite queued files or quantities.
"""

from fastapi import HTTPException
from sqlalchemy import select

from backend.app.core.database import take_write_lock
from backend.app.models.printer import Printer
from backend.app.models.project import Project
from backend.app.utils.model_compatibility import model_compatibility
from backend.app.utils.printer_models import normalize_model_name


def model_key(model):
    return (normalize_model_name(model) or model or "").strip().casefold()


def constrained(policy):
    return bool(policy and (policy.get("models") is not None or policy.get("printer_ids") is not None))


def includes_paused_queues(policy):
    return (policy or {}).get("include_paused_queues") is True


def token(policy):
    from backend.app.services.order_draft import digest

    return digest(policy or {"models": None, "printer_ids": None})


def permits(policy, printer_id, model):
    if not constrained(policy):
        return True
    models, ids = policy.get("models"), policy.get("printer_ids")
    return (models is None or model_key(model) in {model_key(m) for m in models}) and (ids is None or printer_id in ids)


def recipe_permitted(policy, model, *, allow_compatible=False):
    models = (policy or {}).get("models")
    if models is None:
        return True
    return any(
        model_key(model) == model_key(m) or allow_compatible and model_compatibility(model, m) == "compatible"
        for m in models
    )


async def policy_for(db, project_id, *, lock=False, project_line_id=None):
    if project_id is None and project_line_id is not None:
        from backend.app.models.project_line import ProjectLine

        project_id = await db.scalar(select(ProjectLine.project_id).where(ProjectLine.id == project_line_id))
    if project_id is None:
        return None
    if lock:
        await take_write_lock(db, Project.__table__, project_id)
    stmt = select(Project).where(Project.id == project_id).execution_options(populate_existing=True)
    if lock:
        stmt = stmt.with_for_update()
    project = await db.scalar(stmt)
    return project.printer_resources if project else None


async def allowed(db, project_id, printer_id, *, lock=False, project_line_id=None):
    policy = await policy_for(db, project_id, lock=lock, project_line_id=project_line_id)
    if not constrained(policy):
        return True
    from backend.app.services.printer_manager import printer_manager

    printer = await db.get(Printer, printer_id)
    return bool(printer and permits(policy, printer_id, printer_manager.effective_model_for(printer_id, printer.model)))


def refusal():
    return HTTPException(409, {"code": "order_resources_changed", "message": "Order printer restrictions changed"})


async def require_allowed(db, project_id, printer_id, *, lock=False, project_line_id=None):
    if not await allowed(db, project_id, printer_id, lock=lock, project_line_id=project_line_id):
        raise refusal()


async def read(db, project_id, printer_scope=None):
    if printer_scope is not None:
        from backend.app.services.order_plan_access import denied

        raise denied()
    project = await db.get(Project, project_id, populate_existing=True)
    if project is None:
        raise HTTPException(404, "Project not found")
    from backend.app.services.printer_manager import printer_manager

    query = select(Printer).where(Printer.archived.is_(False)).order_by(Printer.name, Printer.id)
    if printer_scope is not None:
        query = query.where(Printer.id.in_(printer_scope))
    printers = list((await db.scalars(query)).all())
    policy = project.printer_resources or {"models": None, "printer_ids": None}
    return {
        "resources": policy,
        "token": token(policy),
        "printers": [
            {
                "id": p.id,
                "name": p.name,
                "model": printer_manager.effective_model_for(p.id, p.model),
                "active": p.is_active,
                "allowed": permits(policy, p.id, printer_manager.effective_model_for(p.id, p.model)),
            }
            for p in printers
        ],
    }


async def update(db, project_id, request, access):
    from backend.app.services import order_plan_access

    user, _scope, printer_scope = await order_plan_access.fresh(db, access)
    if access.actor.kind == "api_key":
        from backend.app.core.auth import authorize_api_key
        from backend.app.services.swap_source_declarations import fresh_api_key

        await authorize_api_key(db, await fresh_api_key(db, access.actor), ["orders:update"])
    if user is not None and not user.has_permission("orders:update"):
        raise order_plan_access.denied()
    await policy_for(db, project_id, lock=True)
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    if project.status != "active":
        raise refusal()
    current = project.printer_resources or {"models": None, "printer_ids": None}
    if token(current) != request.expected_token:
        raise refusal()
    policy = request.resources.model_dump(include={"models", "printer_ids"})
    include_paused = (
        request.resources.include_paused_queues
        if "include_paused_queues" in request.resources.model_fields_set
        else includes_paused_queues(current)
    )
    if include_paused:
        policy["include_paused_queues"] = True
    ids = policy["printer_ids"]
    if ids is not None:
        available = set(
            (await db.scalars(select(Printer.id).where(Printer.id.in_(ids), Printer.archived.is_(False)))).all()
        )
        if available != set(ids) or printer_scope is not None and not set(ids) <= set(printer_scope):
            raise order_plan_access.denied()
    # A printer-scoped key cannot change routing of an order across the whole farm.
    if access.actor.kind == "api_key" and printer_scope is not None:
        raise order_plan_access.denied()
    project.printer_resources = policy if constrained(policy) or include_paused else None
    await db.flush()
    return await read(db, project_id, printer_scope)


async def pending(db, project_id):
    """Address only this order; a line-only legacy reference still belongs to it."""
    from sqlalchemy import or_

    from backend.app.models.auto_queue import AutoQueueItem
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.project_line import ProjectLine
    from backend.app.services.order_queue import awaiting_auto_row_conditions
    from backend.app.services.queue_rebalance import refusal as auto_refusal

    lines = select(ProjectLine.id).where(ProjectLine.project_id == project_id)
    printer = list(
        (
            await db.scalars(
                select(PrintQueueItem)
                .where(
                    or_(PrintQueueItem.project_id == project_id, PrintQueueItem.project_line_id.in_(lines)),
                    PrintQueueItem.status.in_(["pending", "printing"]),
                )
                .order_by(PrintQueueItem.id)
                .limit(10001)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    auto = list(
        (
            await db.scalars(
                select(AutoQueueItem)
                .where(
                    or_(AutoQueueItem.project_id == project_id, AutoQueueItem.project_line_id.in_(lines)),
                    *awaiting_auto_row_conditions(),
                )
                .order_by(AutoQueueItem.id)
                .limit(10001)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    if len(printer) + len(auto) > 10000:
        raise HTTPException(409, {"code": "plan_budget_exceeded"})
    # A claimed preparation without an execution archive has no attributable
    # yield yet. Wait rather than planning its parts a second time.
    from backend.app.models.archive import PrintArchive

    active_archives = set(
        (
            await db.scalars(
                select(PrintArchive.id).where(
                    PrintArchive.id.in_([p.archive_id for p in printer if p.archive_id]),
                    PrintArchive.status == "printing",
                )
            )
        ).all()
    )
    if any(p.status == "printing" and p.archive_id not in active_archives for p in printer):
        raise HTTPException(409, {"code": "order_resources_preparing", "message": "An order print is being prepared"})
    from backend.app.services.filament_policy import queue_policy

    removable_printer = [
        p
        for p in printer
        if p.status == "pending"
        and p.started_at is None
        and p.origin == "queue"
        and p.library_file_id is not None
        and p.project_line_id is not None
        and not p.manual_start
        and p.scheduled_time is None
        and not p.is_calibration
        and queue_policy(p).mode == "auto"
        and not queue_policy(p).review_required
    ]
    removable_auto = [p for p in auto if auto_refusal(p) is None]
    return printer, auto, removable_printer, removable_auto


def queue_basis(printer, auto):
    from backend.app.services.order_draft import digest

    # Options and identity are part of the review, not just the yield/count.
    return digest(
        [
            [
                {
                    c.name: getattr(p, c.name)
                    for c in p.__table__.columns
                    if c.name
                    not in {"waiting_reason", "waiting_reason_code", "waiting_reason_checked_at", "updated_at"}
                }
                for p in tier
            ]
            for tier in (printer, auto)
        ]
    )


async def replan(db, project_id, request, access):
    """Review/dequeue only never-started work; prepare the existing shared draft.

    Publication remains the normal explicit order-plan action. No file capture,
    source deletion, print command, plate acknowledgement or queue pause here.
    """
    import hmac
    from contextlib import AsyncExitStack
    from copy import deepcopy

    from backend.app.models.order_working_plan import OrderWorkingPlan
    from backend.app.schemas.order_working_plan import WorkingWrite
    from backend.app.services import order_draft as draft, order_plan_access, order_working_plan as working
    from backend.app.services.order_metrics import attribute
    from backend.app.services.plan_engine import (
        _current_fleet_capacity,
        counted_parts_by_line,
        plan_lines,
        queued_yield_by_line,
    )
    from backend.app.services.queue_counters import detach_print_queue_refs, update_queue_counters
    from backend.app.services.queue_ops import claim_lanes

    preflight = await pending(db, project_id)
    lanes = {p.queue_id for p in preflight[2]}
    async with AsyncExitStack() as stack:
        if request.apply:
            await stack.enter_async_context(claim_lanes(db, lanes))
            await policy_for(db, project_id, lock=True)
        user, scope, printers = await order_plan_access.fresh(db, access)
        if user is not None and not all(
            user.has_permission(p) for p in ("orders:update", "queue:delete_all", "queue:read_all")
        ):
            raise order_plan_access.denied()
        if access.actor.kind == "api_key":
            from backend.app.core.auth import authorize_api_key
            from backend.app.services.swap_source_declarations import fresh_api_key

            await authorize_api_key(
                db, await fresh_api_key(db, access.actor), ["orders:update", "queue:delete_all", "queue:read_all"]
            )
        if printers is not None:
            # Whole-order queue review cannot be partial or leak hidden lanes.
            raise order_plan_access.denied()
        row = await db.get(OrderWorkingPlan, project_id, populate_existing=True)
        was_missing = row is None
        if request.apply:
            row = await working.lock(db, project_id, create=True)
        revision = 0 if was_missing else row.revision
        if revision != request.expected_revision or row and row.active_operation:
            raise working.refusal()
        data = await draft.inputs(db, project_id)
        if data.ctx.project.status != "active":
            raise working.refusal("order_closed")
        printer, auto, remove_printer, remove_auto = await pending(db, project_id)
        if request.apply and not {p.queue_id for p in remove_printer} <= lanes:
            raise refusal()
        inputs_token = draft.digest([data.token, queue_basis(printer, auto)])
        if request.apply and request.inputs_token != inputs_token:
            raise refusal()

        def visible(file):
            return order_plan_access.visible(file, user, scope)

        if request.apply:
            if request.proposal_entries is None:
                raise refusal()
            proposed = [e.model_dump(mode="json") for e in request.proposal_entries]
        else:
            figures = attribute(data.ctx)[0]
            excluded_auto = tuple(p.id for p in remove_auto)
            excluded_printer = tuple(p.id for p in remove_printer)
            queued = await queued_yield_by_line(
                db,
                data.recipes,
                data.ctx.lines,
                counted_parts_by_line({project_id: figures}),
                exclude_auto_ids=excluded_auto,
                exclude_printer_ids=excluded_printer,
            )
            fleet = await _current_fleet_capacity(
                db,
                exclude_auto_ids=excluded_auto,
                exclude_printer_ids=excluded_printer,
                include_paused_queues=includes_paused_queues(data.ctx.project.printer_resources),
            )
            recipes = {pid: [c for c in candidates if visible(c[1])] for pid, candidates in data.recipes.items()}
            plan = plan_lines(data.ctx, figures, recipes, queued, data.price, fleet)
            if plan.truncated or any(line.unsatisfiable for line in plan.lines):
                raise HTTPException(
                    409,
                    {
                        "code": "order_resources_no_plan",
                        "message": "The selected printers cannot cover the remaining work",
                    },
                )
            proposed = [
                {
                    "entry_id": f"resources-{line.line_id}-{p.plate_id}",
                    "line_id": line.line_id,
                    "plate_id": p.plate_id,
                    "count": p.count,
                    "manual": False,
                    "protected_count": 0,
                    "target": {"kind": "auto"},
                    "line_token": next(item.plan_identity for item in data.ctx.lines if item.id == line.line_id),
                    "source_token": data.source_tokens[p.plate_id],
                }
                for line in plan.lines
                for p in line.rows
            ]
        validated = WorkingWrite(expected_revision=revision, entries=proposed)
        proposed = [e.model_dump(mode="json") for e in validated.entries]
        signature = working.proposal_signature(project_id, request, revision, inputs_token, proposed, access.identity)
        if not request.apply:
            return {
                "proposal_token": signature,
                "inputs_token": inputs_token,
                "expected_revision": revision,
                "entries": proposed,
                "remove_printer_ids": [p.id for p in remove_printer],
                "remove_auto_ids": [p.id for p in remove_auto],
                "preserved_printer_ids": [p.id for p in printer if p not in remove_printer],
                "preserved_auto_ids": [p.id for p in auto if p not in remove_auto],
                "previous_draft_count": sum(e["count"] for e in row.entries) if row else 0,
                "rows": [
                    {
                        "line_id": line.line_id,
                        "filename": p.filename,
                        "printer_model": p.printer_model,
                        "count": p.count,
                    }
                    for line in plan.lines
                    for p in line.rows
                ],
                "count": sum(e["count"] for e in proposed),
            }
        if not request.proposal_token or not hmac.compare_digest(signature, request.proposal_token):
            raise refusal()
        # Existing reference cleanup, no blob unlink; sources remain until GC.
        await detach_print_queue_refs(db, [p.id for p in remove_printer])
        for item in [*remove_printer, *remove_auto]:
            await db.delete(item)
        await db.flush()
        for lane in lanes:
            await update_queue_counters(db, lane)
        data = await draft.inputs(db, project_id)
        entries = working.normalize(data, validated.entries, row.entries, visible)
        plan, coverage, issues = working.projection(data, entries, row.facts, visible=visible)
        if issues:
            raise refusal()
        row.entries = deepcopy(entries)
        row.facts = working.snapshot(data, entries, plan, coverage, issues)
        row.revision = revision + 1
        row.epoch += 1
        row.updated_by = access.actor.label
        await db.flush()
        return {
            "working_plan": await working.state(db, row, data, visible=visible),
            "removed_printer_ids": [p.id for p in remove_printer],
            "removed_auto_ids": [p.id for p in remove_auto],
        }
