"""Move one never-started printer job, without consuming an order plan again.

Preflight closes before storage -> lane -> order -> rows. File parsing happens
only in preflight; final admission compares the captured basis under the lane.
"""

import json
import logging
from copy import deepcopy
from dataclasses import asdict

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from backend.app.core import database
from backend.app.core.permissions import Permission
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.calibration_session import CalibrationSession
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.queue_source import STATE_READY, QueueSource
from backend.app.models.user import User
from backend.app.schemas.calibration_mode import normalize_mode
from backend.app.services import (
    order_filing,
    order_resources,
    print_state_recovery_preview as previews,
    queue_sources,
    swap_compatibility,
)
from backend.app.services.auto_queue_scheduler import compose_options
from backend.app.services.filament_intake import item_descriptor, read_item_requirements
from backend.app.services.filament_policy import decode, queue_policy
from backend.app.services.print_option_defaults import preference_options
from backend.app.services.queue_counters import update_queue_counters
from backend.app.services.queue_ops import claim_lanes
from backend.app.services.swap_source_declarations import fresh_api_key

logger = logging.getLogger(__name__)
_MODE_FIELDS = ("bed_levelling", "flow_cali", "nozzle_offset_cali")
_DEFAULTS = {
    "bed_levelling": True,
    "flow_cali": True,
    "nozzle_offset_cali": True,
    "layer_inspect": False,
    "timelapse": False,
    "timelapse_storage": None,
    "mesh_mode_fast_check": True,
    "gcode_injection": False,
    "preheat_override": "inherit",
    "preheat_chamber_target_override": None,
}
_CARRY = (
    "project_id",
    "project_line_id",
    "created_by_id",
    "scheduled_time",
    "auto_off_after",
    "require_previous_success",
    "batch_id",
    "auto_eject",
    "auto_eject_settings",
    "nozzle_mapping",
)


def refusal(code, status=409):
    return HTTPException(status, {"code": code})


async def _rights(db, actor, item):
    """No last_used writer or request-cached roles inside the admission transaction."""
    if actor.kind == "api_key":
        from backend.app.core.auth import authorize_api_key

        key = await fresh_api_key(db, actor)
        if key.printer_ids is not None:
            raise refusal("transfer_forbidden", 403)
        await authorize_api_key(
            db,
            key,
            [
                Permission.QUEUE_CREATE.value,
                Permission.QUEUE_DELETE_ALL.value,
                Permission.QUEUE_READ_ALL.value,
                Permission.QUEUE_READ.value,
            ],
        )
        return
    user = await db.scalar(
        select(User)
        .options(selectinload(User.groups))
        .where(User.id == actor.user_id)
        .execution_options(populate_existing=True)
    )
    if user is None or not user.is_active:
        raise refusal("transfer_forbidden", 403)
    mine = item.created_by_id is not None and item.created_by_id == user.id
    if not user.has_permission(Permission.QUEUE_CREATE.value) or not user.has_permission(Permission.QUEUE_READ.value):
        raise refusal("transfer_forbidden", 403)
    for all_perm, own_perm in (
        (Permission.QUEUE_READ_ALL, Permission.QUEUE_READ_OWN),
        (Permission.QUEUE_DELETE_ALL, Permission.QUEUE_DELETE_OWN),
    ):
        if not (user.has_permission(all_perm.value) or mine and user.has_permission(own_perm.value)):
            raise refusal("transfer_forbidden", 403)


def _row_basis(row):
    # All persistent columns, not relationships or cached DTOs. This fences
    # changes to position/options as well as reuse of SQLite integer ids.
    return {column.key: getattr(row, column.key) for column in row.__table__.columns}


async def _navigation_links(db, item):
    """Keep live navigation without waiting behind a source purge's row lock.

    Archive purges lock the archive before jobs. We already hold the job, so
    a blocking FK KEY SHARE here would invert that order. SKIP LOCKED makes
    that optional link NULL instead; bytes/provenance never depend on it.
    """
    links = {}
    for field, model in (("archive_id", PrintArchive), ("library_file_id", LibraryFile)):
        source_id = getattr(item, field)
        links[field] = (
            await db.scalar(
                select(model.id)
                .where(model.id == source_id)
                .with_for_update(read=True, key_share=True, skip_locked=True)
            )
            if source_id is not None
            else None
        )
    return links


async def _facts(db, item_id, actor, *, lock=False):
    query = select(PrintQueueItem).where(PrintQueueItem.id == item_id).execution_options(populate_existing=True)
    item = await db.scalar(query.with_for_update() if lock else query)
    if item is None:
        raise refusal("transfer_gone", 404)
    await _rights(db, actor, item)
    queue = await db.get(PrinterQueue, item.queue_id, populate_existing=True)
    printer = await db.get(Printer, queue.printer_id) if queue else None
    if queue is None or printer is None or printer.archived:
        raise refusal("transfer_gone", 404)
    if (
        item.status != "pending"
        or item.started_at is not None
        or item.completed_at is not None
        or item.dispatch_attempts != 0
        or item.origin != "queue"
        or item.is_calibration
        or item.calibration_session_id is not None
        or queue.current_item_id == item.id
    ):
        raise refusal("transfer_started")
    if not item.swap_declaration_identity:
        raise refusal("transfer_identity_missing")
    if await db.scalar(select(CalibrationSession.id).where(CalibrationSession.print_queue_item_id == item.id).limit(1)):
        raise refusal("transfer_started")
    source = await db.get(QueueSource, item.queue_source_id, populate_existing=True) if item.queue_source_id else None
    snapshot = item.source_snapshot
    if (
        source is None
        or source.state != STATE_READY
        or not isinstance(snapshot, dict)
        or type(snapshot.get("version")) is not int
        or snapshot.get("version") != 1
        or snapshot.get("format") != "3mf"
        or swap_compatibility.snapshot_fact_malformed(snapshot)
    ):
        raise refusal("transfer_source_unavailable")
    descriptor = await item_descriptor(db, item, fresh=True)
    if descriptor is None:
        raise refusal("transfer_source_unavailable")
    if item.nozzle_rack_choice is not None:
        raise refusal("transfer_rack_choice")
    policy = queue_policy(item)
    if policy.review_required:
        raise refusal("transfer_policy_invalid")
    query = select(AutoQueueItem).where(AutoQueueItem.assigned_to_item_id == item.id)
    backrefs = list((await db.scalars(query)).all())
    ancestor = None
    if item.source_auto_item_id is not None:
        query = (
            select(AutoQueueItem)
            .where(AutoQueueItem.id == item.source_auto_item_id)
            .execution_options(populate_existing=True)
        )
        ancestor = await db.scalar(query.with_for_update() if lock else query)
        if (
            ancestor is None
            or ancestor.status != "assigned"
            or ancestor.assigned_to_item_id != item.id
            or [a.id for a in backrefs] != [ancestor.id]
            or not ancestor.swap_declaration_identity
            or ancestor.queue_source_id != item.queue_source_id
            or (ancestor.project_id, ancestor.project_line_id, ancestor.created_by_id)
            != (item.project_id, item.project_line_id, item.created_by_id)
        ):
            raise refusal("transfer_ancestor_changed")
        other = await item_descriptor(db, ancestor, fresh=True)
        if other is None or other.sha256 != descriptor.sha256 or other.swap_fact != descriptor.swap_fact:
            raise refusal("swap_declaration_changed")
        intent = decode(item.filament_routing, {})
        if not isinstance(intent, dict) or intent.get("resolved_plate_id") != item.plate_id:
            raise refusal("transfer_plate_changed")
        if ancestor.plate_id is not None and ancestor.plate_id != item.plate_id:
            raise refusal("transfer_plate_changed")
    elif backrefs:
        raise refusal("transfer_ancestor_changed")
    await order_filing.resolve_link(db, item.project_id, item.project_line_id)
    return item, queue, printer, source, descriptor, ancestor, policy


async def _options(db, item, ancestor, model):
    owner = await db.get(User, item.created_by_id, populate_existing=True) if item.created_by_id else None
    profile = await preference_options(db, owner, model)
    try:
        composed = compose_options(profile, ancestor.order_print_options if ancestor else None, model, model)
    except ValueError:
        raise refusal("transfer_profile_invalid") from None
    resolved = {key: composed.get(key, default) for key, default in _DEFAULTS.items()}
    for field in _MODE_FIELDS:
        resolved[field] = normalize_mode(resolved[field])
    current = {key: getattr(item, key) for key in _DEFAULTS}
    for field in _MODE_FIELDS:
        current[field] = getattr(item, field + "_mode") or normalize_mode(current[field])
    diff = [
        {"field": key, "before": current[key], "after": value}
        for key, value in resolved.items()
        if current[key] != value
    ]
    return composed, diff, owner is None or profile is None


async def _basis(db, facts, actor, model):
    item, queue, printer, source, descriptor, ancestor, _ = facts
    options, diff, default_profile = await _options(db, item, ancestor, model)
    basis = {
        "job": _row_basis(item),
        "ancestor": _row_basis(ancestor) if ancestor else None,
        "queue": [queue.id, queue.printer_id, queue.created_at, printer.serial_number],
        "blob": _row_basis(source),
        "fact": asdict(descriptor.swap_fact),
        "options": options,
        "model": model,
        "actor": [actor.kind, actor.user_id, actor.key_id],
        "epoch": queue_sources.current_epoch(),
    }
    return previews.digest(basis), diff, default_profile


async def _preflight(item_id, actor):
    async with database.async_session() as db:
        facts = await _facts(db, item_id, actor)
        item, queue, printer, source, descriptor, ancestor, policy = facts
        req = await read_item_requirements(db, item)
        if req.status != "ok" or not req.model or not req.resolved_plate_id or not req.source_identity:
            raise refusal("transfer_source_unavailable")
        if ancestor is not None:
            ancestor_req = await read_item_requirements(db, ancestor)
            if ancestor_req.status != "ok" or ancestor_req.resolved_plate_id != req.resolved_plate_id:
                raise refusal("transfer_plate_changed")
        basis, diff, default_profile = await _basis(db, facts, actor, req.model)
        return {
            "item_id": item.id,
            "queue_id": queue.id,
            "project_id": item.project_id,
            "project_line_id": item.project_line_id,
            "basis": basis,
            "requirements": req,
            "display_name": descriptor.display_filename,
            "plate_id": item.plate_id,
            "printer_name": printer.name,
            "target_model": ancestor.target_model if ancestor else req.model,
            "target_location_id": ancestor.target_location_id if ancestor else None,
            "manual_start": item.manual_start,
            "release_pins": policy.mode == "pinned",
            "option_changes": diff,
            "default_profile": default_profile,
            "comparison_model": req.model,
            "mode": "rearmed" if ancestor else "created",
        }


async def preview(item_id, actor):
    try:
        result = await _preflight(item_id, actor)
    except HTTPException as exc:
        if exc.status_code in (403, 404):
            raise
        return {"allowed": False, "refusal": exc.detail["code"]}
    public = {
        key: value
        for key, value in result.items()
        if key not in ("requirements", "basis", "project_id", "project_line_id")
    }
    return {
        **public,
        "allowed": True,
        "token": previews.sign(
            {
                "action": "queue_auto_transfer",
                "item_id": item_id,
                "basis": result["basis"],
                "actor": [actor.kind, actor.user_id, actor.key_id],
                "epoch": queue_sources.current_epoch(),
            }
        ),
    }


async def apply(item_id, actor, payload):
    try:
        token = previews.verify(payload.token)
    except ValueError:
        raise refusal("transfer_stale") from None
    if (
        token.get("action") != "queue_auto_transfer"
        or token.get("item_id") != item_id
        or token.get("actor") != [actor.kind, actor.user_id, actor.key_id]
        or token.get("epoch") != queue_sources.current_epoch()
    ):
        raise refusal("transfer_stale")
    pre = await _preflight(item_id, actor)
    if token.get("basis") != pre["basis"]:
        raise refusal("transfer_stale")
    if pre["manual_start"] and not payload.allow_automatic_start:
        raise refusal("transfer_manual_consent")
    if pre["release_pins"] and not payload.allow_automatic_filament:
        raise refusal("transfer_pins_consent")
    if pre["option_changes"] and not payload.accept_profile_changes:
        raise refusal("transfer_profile_consent")
    req = pre["requirements"]
    async with queue_sources.storage_mutation(), database.async_session() as db, claim_lanes(db, [pre["queue_id"]]):
        await order_resources.policy_for(db, pre["project_id"], project_line_id=pre["project_line_id"], lock=True)
        facts = await _facts(db, item_id, actor, lock=True)
        item, queue, printer, source, descriptor, ancestor, policy = facts
        if (item.queue_id, item.project_id, item.project_line_id) != (
            pre["queue_id"],
            pre["project_id"],
            pre["project_line_id"],
        ):
            raise refusal("transfer_stale")
        basis, _, _ = await _basis(db, facts, actor, req.model)
        if basis != token["basis"]:
            raise refusal("transfer_stale")
        position = (
            await db.scalar(select(func.max(AutoQueueItem.position)).where(AutoQueueItem.status == "pending")) or 0
        ) + 1
        if ancestor is None:
            navigation = await _navigation_links(db, item)
            ancestor = AutoQueueItem(
                **navigation,
                queue_source_id=source.id,
                source_snapshot=deepcopy(item.source_snapshot),
                target_model=req.model,
                target_location_id=None,
                plate_id=item.plate_id,
                required_filament_types=json.dumps(sorted({f["type"] for f in req.used_filaments})),
                print_time_seconds=req.print_time_seconds,
                feed_policy=policy.feed_policy,
                force_color_match=policy.force_color_match,
                allow_base_material_match=policy.allow_base_material_match,
                filament_overrides=json.dumps(list(policy.filament_overrides)),
            )
            db.add(ancestor)
        for field in _CARRY:
            setattr(ancestor, field, deepcopy(getattr(item, field)))
        ancestor.position = position
        ancestor.status = "pending"
        ancestor.manual_start = False
        ancestor.waiting_reason = None
        ancestor.assigned_to_item_id = None
        ancestor.assigned_at = None
        ancestor.cancelled_at = None
        await db.flush()
        result = {"old_item_id": item.id, "resulting_auto_item_id": ancestor.id, "mode": pre["mode"]}
        item.source_auto_item_id = None
        await db.delete(item)
        await update_queue_counters(db, queue.id)
        await db.commit()
    logger.info(
        "Moved printer job %s to auto job %s actor=%s:%s mode=%s",
        item_id,
        result["resulting_auto_item_id"],
        actor.kind,
        actor.user_id,
        result["mode"],
    )
    from backend.app.core.websocket import ws_manager

    await ws_manager.broadcast({"type": "queue_changed"})
    return result
