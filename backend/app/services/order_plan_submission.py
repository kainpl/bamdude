"""Durable, addressed consumption at the existing queue publication commit."""

import logging
import traceback
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.app.models.order_working_plan import OrderPlanReceipt, OrderPlanSubmission
from backend.app.models.printer import Printer
from backend.app.schemas.order_auto_eject import AutoEjectSettings
from backend.app.schemas.order_print_options import OrderPrintOptions
from backend.app.services import order_draft, order_plan_access as authority, order_working_plan as working
from backend.app.services.order_print_options import override_options
from backend.app.services.print_option_defaults import preference_options
from backend.app.utils.model_compatibility import model_compatibility

logger = logging.getLogger(__name__)
EXPECTED_FAILURES = frozenset(
    {
        "plan_conflict",
        "inputs_changed",
        "entry_changed",
        "source_review_required",
        "source_changed",
        "source_unavailable",
        "source_copy_failed",
        "source_copy_busy",
        "source_spool_replaced",
        "source_unreadable",
        "source_invalid",
        "source_copy_timeout",
        "source_spool_no_space",
        "source_spool_write_failed",
        "order_closed",
        "line_unavailable",
        "target_unavailable",
        "target_incompatible",
        "plan_printer_scope",
        "plan_authority_changed",
        "operation_actor_changed",
        "submission_cancelled",
        "publication_count_changed",
    }
)


def publication_failure(error):
    from backend.app.services.queue_sources import QueueSourceError

    if isinstance(error, QueueSourceError) and error.reason in EXPECTED_FAILURES:
        return error.reason
    if isinstance(error, HTTPException):
        code = error.detail.get("code") if isinstance(error.detail, dict) else error.detail
        return code if isinstance(code, str) and code in EXPECTED_FAILURES else "publication_refused"
    return "publication_failed"


@dataclass(frozen=True)
class PlanConsumption:
    plan_id: int
    operation_id: str
    entry_id: str
    access: authority.PlanAccess


@dataclass(frozen=True)
class LegacyPlanPublication:
    """A compatibility caller may publish only while no shared draft exists."""

    plan_id: int
    capture: object | None = None


@dataclass
class ConsumptionTicket:
    plan: object
    operation: OrderPlanSubmission
    part: dict


class AlreadyPublished(Exception):
    """The staged duplicate must roll back; its first receipt is authoritative."""

    def __init__(self, outcome):
        self.outcome = outcome


def target_scope(entry, printers):
    target = entry.get("target", {"kind": "auto"})
    if printers is not None and (target["kind"] == "auto" or target.get("printer_id") not in printers):
        raise HTTPException(403, {"code": "plan_printer_scope"})


async def _operation(db, plan_id, operation_id, *, lock=False):
    query = select(OrderPlanSubmission).where(OrderPlanSubmission.id == operation_id)
    if lock:
        query = query.with_for_update()
    operation = await db.scalar(query.execution_options(populate_existing=True))
    if operation is None or operation.plan_id != plan_id:
        raise HTTPException(404, "Submission not found")
    return operation


async def _out(db, operation):
    receipts = list(
        (
            await db.scalars(
                select(OrderPlanReceipt)
                .where(OrderPlanReceipt.operation_id == operation.id)
                .order_by(OrderPlanReceipt.created_at, OrderPlanReceipt.id)
            )
        ).all()
    )
    done = {r.entry_id for r in receipts}
    return {
        "operation_id": operation.id,
        "status": operation.status,
        "created_count": sum(r.count for r in receipts),
        "remaining_count": sum(p["count"] for p in operation.request["parts"] if p["entry"]["entry_id"] not in done),
        "created": [deepcopy(r.outcome) for r in receipts],
        "remaining": [
            {"entry_id": p["entry"]["entry_id"], "count": p["count"]}
            for p in operation.request["parts"]
            if p["entry"]["entry_id"] not in done
        ],
    }


async def read(db, plan_id, operation_id, access):
    _, _, printers = await authority.fresh(db, access, edit=False, receipts=True)
    operation = await _operation(db, plan_id, operation_id)
    if operation.actor != access.identity:
        await admin_cancellation_access(db, operation, access)
        return {**await _out(db, operation), "can_execute": False, "can_cancel": True, "is_foreign": True}
    for part in operation.request["parts"]:
        target_scope(part["entry"], printers)
    return await _out(db, operation)


async def admin_cancellation_access(db, operation, access):
    """Permission to cancel a remainder never transfers publication credentials."""
    from backend.app.models.library import LibraryFile
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.printer_queue import PrinterQueue

    user, scope, printers = await authority.fresh(db, access, edit=False, receipts=True, admin_cancel=True)
    if scope == "none":
        raise authority.denied()
    for part in operation.request["parts"]:
        target_scope(part["entry"], printers)
        source_id = part.get("library_file_id")
        # Older operations lack the source's durable ID. Only a reader of ALL
        # files can authorize their cancellation; an alias/hash is not ownership.
        if scope != "all":
            file = await db.get(LibraryFile, source_id, populate_existing=True) if source_id else None
            if file is None or user is None or file.created_by_id != user.id:
                raise authority.denied()
    if printers is not None:
        receipts = list(await db.scalars(select(OrderPlanReceipt).where(OrderPlanReceipt.operation_id == operation.id)))
        ids = [i for r in receipts if r.outcome.get("queue_kind") == "printer" for i in r.outcome["queue_item_ids"]]
        actual = set(
            await db.scalars(
                select(PrinterQueue.printer_id)
                .join(PrintQueueItem, PrintQueueItem.queue_id == PrinterQueue.id)
                .where(PrintQueueItem.id.in_(ids))
            )
        )
        if not actual <= printers:
            raise authority.denied()
    return user


async def register(db, plan_id, request, access):
    await authority.guard(db, access)
    plan = await working.lock(db, plan_id)
    if plan is None:
        raise working.refusal()
    user, scope, printers = await authority.fresh(db, access, execute=True, receipts=True)
    digest = order_draft.digest(
        [plan_id, request.expected_revision, sorted((p.entry_id, p.count) for p in request.parts)]
    )
    previous = await db.get(OrderPlanSubmission, request.operation_id, populate_existing=True)
    if previous is not None:
        if previous.plan_id != plan_id or previous.actor != access.identity or previous.request_digest != digest:
            raise working.refusal("operation_conflict")
        for part in previous.request["parts"]:
            target_scope(part["entry"], printers)
        return await _out(db, previous)
    working.check(plan, request.expected_revision)
    data = await order_draft.inputs(db, plan_id)
    if data.ctx.project.status != "active":
        raise working.refusal("order_closed")
    entries = {e["entry_id"]: e for e in plan.entries}
    parts = []
    overrides = OrderPrintOptions(overrides=data.ctx.project.order_print_options or {}).model_dump()["overrides"]
    for requested in request.parts:
        entry = entries.get(requested.entry_id)
        if entry is None or requested.count > entry["count"]:
            raise working.refusal("entry_changed")
        target_scope(entry, printers)

        def visible(file):
            return authority.visible(file, user, scope)

        exact, _, issues = working.projection(data, [entry], plan.facts, visible=visible)
        if issues:
            raise working.refusal("source_review_required")
        selected = next(row for line in exact.lines for row in line.rows)
        printer = (
            await db.get(Printer, entry["target"]["printer_id"], populate_existing=True)
            if entry["target"]["kind"] == "printer"
            else None
        )
        if entry["target"]["kind"] == "printer" and (printer is None or printer.archived):
            raise working.refusal("target_unavailable")
        model = printer.model if printer else selected.printer_model
        if printer and model_compatibility(selected.printer_model, model) == "incompatible":
            raise working.refusal("target_incompatible")
        profile = await preference_options(db, user, model)
        options = profile.for_printer_queue() if profile and printer else profile.for_auto_queue() if profile else {}
        try:
            options.update(override_options(overrides, selected.printer_model, model if printer else None))
        except ValueError as exc:
            raise HTTPException(422, "Order print options do not fit the target printer") from exc
        file = next(
            file
            for candidates in data.recipes.values()
            for plate, file, _ in candidates
            if plate.id == entry["plate_id"]
        )
        parts.append(
            {
                "entry": deepcopy(entry),
                "count": requested.count,
                "expected_hash": file.file_hash,
                "library_file_id": file.id,
                "frozen_options": {"options": options, "order_overrides": overrides},
                "auto_eject": {
                    "enabled": bool(data.ctx.project.auto_eject_enabled),
                    "settings": AutoEjectSettings.model_validate(
                        data.ctx.project.auto_eject_settings or {}
                    ).model_dump(),
                },
            }
        )
    operation = OrderPlanSubmission(
        id=request.operation_id,
        plan_id=plan_id,
        actor=access.identity,
        request_digest=digest,
        request={"schema_version": 1, "parts": parts, "actor_label": access.actor.label},
    )
    db.add(operation)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise working.refusal("operation_conflict") from exc
    plan.active_operation = operation.id
    await db.flush()
    return await _out(db, operation)


async def claim(db, context: PlanConsumption | LegacyPlanPublication, sha256: str):
    """Called after the publisher's existing source/lane guards, before rows."""
    from backend.app.models.project import Project
    from backend.app.models.project_line import ProjectLine

    if isinstance(context, LegacyPlanPublication):
        from backend.app.core.database import take_write_lock
        from backend.app.models.order_working_plan import OrderWorkingPlan

        if context.capture is not None:
            from backend.app.core.auth import authorize_api_key
            from backend.app.core.auth_lifetime import validate_captured

            principal = await validate_captured(db, context.capture, required_permissions=("queue:create",))
            if principal.api_key is not None:
                await authorize_api_key(
                    db, principal.api_key, ["orders:update", "orders:file_prints"], require_any=True
                )
            elif not any(principal.user.has_permission(p) for p in ("orders:update", "orders:file_prints")):
                raise authority.denied()

        # The new draft's FK takes KEY SHARE here. An exclusive order lock
        # serializes the absence check with first-save, after source capture
        # and before any queue insert, rather than trusting route preflight.
        await take_write_lock(db, Project.__table__, context.plan_id)
        project = await db.scalar(select(Project).where(Project.id == context.plan_id).with_for_update())
        if project is None or project.status != "active":
            raise working.refusal("order_closed")
        if await db.get(OrderWorkingPlan, context.plan_id, populate_existing=True) is not None:
            raise working.refusal("working_plan_required")
        return None

    # Queue inserts take these FK key-share locks anyway. Acquire them BEFORE
    # draft: stock/lifecycle writers can already hold a line FOR UPDATE and
    # later fence its draft. Taking the implicit FK lock after draft reverses
    # that order. NO KEY UPDATE on the order remains compatible with key share.
    before = await _operation(db, context.plan_id, context.operation_id)
    intended = next((p for p in before.request["parts"] if p["entry"]["entry_id"] == context.entry_id), None)
    if intended is None:
        raise working.refusal("entry_changed")
    await db.execute(select(Project.id).where(Project.id == context.plan_id).with_for_update(read=True, key_share=True))
    line = await db.scalar(
        select(ProjectLine.id)
        .where(ProjectLine.id == intended["entry"]["line_id"], ProjectLine.project_id == context.plan_id)
        .with_for_update(read=True, key_share=True)
    )
    if line is None:
        raise working.refusal("line_unavailable")
    plan = await working.lock(db, context.plan_id)
    operation = await _operation(db, context.plan_id, context.operation_id, lock=True)
    user, scope, printers = await authority.fresh(db, context.access, execute=True)
    if operation.actor != context.access.identity:
        raise working.refusal("operation_actor_changed")
    part = next((p for p in operation.request["parts"] if p["entry"]["entry_id"] == context.entry_id), None)
    if part is None:
        raise working.refusal("entry_changed")
    target_scope(part["entry"], printers)
    previous = await db.get(OrderPlanReceipt, f"{operation.id}:{context.entry_id}")
    if previous:
        raise AlreadyPublished(deepcopy(previous.outcome))
    if plan is None or plan.active_operation != operation.id or operation.status != "pending":
        raise working.refusal("submission_cancelled")
    entry = next((e for e in plan.entries if e["entry_id"] == context.entry_id), None)
    if (
        entry is None
        or entry["count"] < part["count"]
        or entry.get("source_token") != part["entry"].get("source_token")
    ):
        raise working.refusal("entry_changed")
    data = await order_draft.inputs(db, context.plan_id)
    if data.ctx.project.status != "active":
        raise working.refusal("order_closed")
    exact, _, issues = working.projection(
        data, [entry], plan.facts, visible=lambda file: authority.visible(file, user, scope)
    )
    if issues or part["expected_hash"] and part["expected_hash"] != sha256:
        raise working.refusal("source_changed")
    if entry["target"]["kind"] == "printer":
        from backend.app.services.order_resources import require_allowed

        await require_allowed(db, context.plan_id, entry["target"]["printer_id"])
        printer = await db.get(Printer, entry["target"]["printer_id"], populate_existing=True)
        selected = next(row for line in exact.lines for row in line.rows)
        if (
            printer is None
            or printer.archived
            or model_compatibility(selected.printer_model, printer.model) == "incompatible"
        ):
            raise working.refusal("target_unavailable")
    return ConsumptionTicket(plan, operation, part)


async def consume(db, ticket: ConsumptionTicket, queue_ids, kind):
    """Same transaction as queue rows; no HTTP result is a consumption proof."""
    plan, operation, part = ticket.plan, ticket.operation, ticket.part
    if len(queue_ids) != part["count"]:
        raise working.refusal("publication_count_changed")
    entry = part["entry"]
    outcome = {
        "entry_id": entry["entry_id"],
        "line_id": entry["line_id"],
        "plate_id": entry["plate_id"],
        "count": part["count"],
        "queue_kind": kind,
        "queue_item_ids": list(queue_ids),
    }
    db.add(
        OrderPlanReceipt(
            id=f"{operation.id}:{entry['entry_id']}",
            operation_id=operation.id,
            entry_id=entry["entry_id"],
            count=part["count"],
            outcome=outcome,
        )
    )
    remaining = deepcopy(plan.entries)
    current = next(e for e in remaining if e["entry_id"] == entry["entry_id"])
    current["count"] -= part["count"]
    current["protected_count"] = max(0, current.get("protected_count", 0) - part["count"])
    if current["count"] > 0 and current["protected_count"] == 0:
        current["manual"] = False
    if current["count"] == 0:
        remaining.remove(current)
    plan.entries = remaining
    plan.revision += 1
    if not operation.epoch_advanced:
        plan.epoch += 1
        operation.epoch_advanced = True
    await db.flush()
    receipts = set(
        (await db.scalars(select(OrderPlanReceipt.entry_id).where(OrderPlanReceipt.operation_id == operation.id))).all()
    )
    if len(receipts) == len(operation.request["parts"]):
        operation.status = "complete"
        plan.active_operation = None
    await db.flush()


async def cancel(db, plan_id, operation_id, access, *, administrative=False, reason=None):
    await authority.guard(db, access)
    plan = await working.lock(db, plan_id)
    user, _, printers = await authority.fresh(db, access, receipts=True)
    operation = await _operation(db, plan_id, operation_id, lock=True)
    if administrative:
        if not reason or not reason.strip():
            raise HTTPException(422, "A cancellation reason is required")
        user = await admin_cancellation_access(db, operation, access)
    elif operation.actor != access.identity:
        raise working.refusal("operation_actor_changed")
    for part in operation.request["parts"]:
        target_scope(part["entry"], printers)
    if operation.status == "pending":
        before = await _out(db, operation)
        cancellation = {
            "actor": access.identity,
            "label": access.actor.label,
            "original_actor": operation.actor,
            "original_author": operation.request.get("actor_label"),
            "reason": reason.strip() if reason else "author_cancelled",
            "parts": before["remaining"],
        }
        operation.request = {**deepcopy(operation.request), "cancellation": cancellation}
        operation.status = "cancelled"
        if plan and plan.active_operation == operation.id:
            plan.active_operation = None
            plan.revision += 1
        from backend.app.services import order_journal

        await order_journal.record(
            db,
            plan_id,
            "plan_submission_cancelled",
            {
                "operation_id": operation_id,
                "original_actor": operation.actor,
                "count": sum(p["count"] for p in before["remaining"]),
                "original_author": operation.request.get("actor_label"),
                "note": reason.strip() if reason else None,
            },
            actor=user,
        )
    await db.flush()
    return await _out(db, operation)


async def fence(db, plan_id):
    """Closing an order fences pending capture, without changing existing queues."""
    plan = await working.lock(db, plan_id)
    if plan and plan.active_operation:
        operation = await _operation(db, plan_id, plan.active_operation, lock=True)
        operation.status = "cancelled"
        plan.active_operation = None
        plan.revision += 1
        await db.flush()


async def execute(db, plan_id, operation_id, access, publish):
    # An explicit continuation is required after reconnect/restart. Never hold
    # a draft transaction while the existing writer captures source bytes.
    state = await read(db, plan_id, operation_id, access)
    if state.get("is_foreign"):
        raise working.refusal("operation_actor_changed")
    if state["status"] != "pending":
        return state
    operation = await _operation(db, plan_id, operation_id)
    parts = deepcopy(operation.request["parts"])
    done = {r["entry_id"] for r in state["created"]}
    user, _, _ = await authority.fresh(db, access, execute=True, receipts=True)
    from backend.app.models.user import User

    actor = User(id=user.id, username=user.username) if user else None
    if actor is not None:
        actor._auth_capture = access.actor.capture
    await db.rollback()
    for part in parts:
        entry = part["entry"]
        if entry["entry_id"] in done:
            continue
        context = PlanConsumption(plan_id, operation_id, entry["entry_id"], access)
        try:
            await publish(part, context, actor)
            await db.commit()
        except AlreadyPublished:
            await db.rollback()
        except Exception as error:
            reason = publication_failure(error)
            # Never format the exception or exc_info: SQLAlchemy exceptions
            # carry statement parameters, and source errors may include paths.
            frames = traceback.extract_tb(error.__traceback__)
            where = ",".join(f"{Path(f.filename).name}:{f.name}:{f.lineno}" for f in frames[-4:])
            logger.log(
                logging.ERROR if reason == "publication_failed" else logging.WARNING,
                "Plan publication refused operation=%s entry=%r reason=%s error_type=%s where=%s",
                operation_id,
                entry["entry_id"],
                reason,
                type(error).__name__,
                where,
            )
            await db.rollback()
            # Receipts, not a transport response, describe partial success.
            state = await read(db, plan_id, operation_id, access)
            if state["status"] == "pending":
                state["error"] = reason
                state["error_entry_id"] = entry["entry_id"]
            return state
    return await read(db, plan_id, operation_id, access)
