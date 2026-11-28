"""Per-plate hardware compatibility, using the same reader and resolver as dispatch."""

import logging
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select

from backend.app.core.permissions import Permission
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.settings import Settings
from backend.app.services import queue_sources
from backend.app.services.filament_intake import (
    enrich_family_filament_types,
    item_descriptor,
    item_source,
    resolve_source_path,
    routing_detail,
)
from backend.app.services.filament_policy import choices_policy, decode, queue_policy
from backend.app.services.filament_preflight import ranked_feed
from backend.app.services.filament_requirements import PrintRequirementsCache
from backend.app.services.filament_routing import (
    backup_twins,
    channel_nozzle_counts,
    channel_refusal,
    effective_slots,
    normalized_color,
    resolve_filament_routing,
    slot_nozzle,
    source_fits,
)
from backend.app.services.printer_location_service import load_tree, subtree_ids
from backend.app.services.printer_manager import printer_manager
from backend.app.utils.model_compatibility import model_compatibility
from backend.app.utils.printer_configs import requires_left_tpu_firmware_check
from backend.app.utils.printer_models import is_dual_nozzle_model, normalize_model_name

logger = logging.getLogger(__name__)
_CHANNEL_SOURCE_LIMIT = 8


def explain_channels(req, policy, snapshot, result):
    """Bounded diagnostics for this printer, using the actual routing inputs."""
    if not (result.plan or result.slots):
        return []
    slots = effective_slots(req, policy)
    counts = channel_nozzle_counts(slots)
    left_tpu_check = requires_left_tpu_firmware_check(snapshot.model)
    channels = []
    for slot in slots:
        sid = slot["slot_id"]
        nozzle = slot_nozzle(slot)
        pin = policy.physical_pins.get(sid)
        planned = result.plan.assignments[sid] if result.plan else None
        selected = planned.id if planned else pin["source_id"] if pin else None
        required_color = normalized_color(slot.get("color"))
        refusal = channel_refusal(req, slot, snapshot)
        if planned:
            sources = (planned,)
        elif pin:
            allowed = {selected} | backup_twins(selected, snapshot)
            sources = tuple(source for source in snapshot.sources if source.id in allowed)
        else:
            sources = snapshot.sources
        checks = []
        for source in sources:
            if refusal:
                fits, why, unknown = False, refusal.reason, refusal.status == "unknown"
            elif planned:
                fits, why, unknown = True, None, False
            else:
                fits, why, unknown = source_fits(
                    slot,
                    source,
                    policy,
                    snapshot,
                    nozzle=nozzle,
                    nozzle_counts=counts,
                    allowed=None,
                    pin=pin,
                    left_tpu_check=left_tpu_check,
                    explain=True,
                )
            checks.append(
                {
                    "source_id": source.id,
                    "kind": source.kind,
                    "material": source.material,
                    "reported_material": source.reported_material,
                    "profile": source.rule_variant,
                    "status": "compatible" if fits else "unknown" if unknown else "incompatible",
                    "reason": why,
                    "color_matches": bool(required_color and required_color == normalized_color(source.rule_color)),
                    # A direct rack (H3C, P3C): the source as people read it (``AMS-3H 5``,
                    # ``Ext-3``) and the PrintTool it prints through — the server's words,
                    # the dialog computes none. None on every other printer.
                    "label": snapshot.layout.rack_label(source.id),
                    "print_tool": source.rack_addr + 1 if source.rack_addr is not None else None,
                    "feed_block": source.feed_block,
                }
            )
        checks.sort(
            key=lambda check: (
                check["source_id"] != selected,
                check["kind"] != "external",
                check["status"] != "compatible",
                check["source_id"],
            )
        )
        channel_result = refusal or (result if sid in result.slots or planned else None)
        channels.append(
            {
                "slot_id": sid,
                "material": slot["type"],
                "profile": slot.get("tray_info_idx"),
                "selected_source_id": selected,
                # A direct rack's notices about this channel (manual feed), worded here.
                "notices": [render_notice(notice) for notice in result.notices if notice.get("slot") == sid],
                "status": channel_result.status if channel_result else None,
                "reason": routing_detail(channel_result.reason) if channel_result and channel_result.reason else None,
                "sources": [
                    {**check, "reason": routing_detail(check["reason"]) if check["reason"] else None}
                    for check in checks[:_CHANNEL_SOURCE_LIMIT]
                ],
                "omitted_sources": max(0, len(checks) - _CHANNEL_SOURCE_LIMIT),
            }
        )
    return channels


def render_notice(notice: dict) -> dict:
    """A plan's notice (``filament_routing.rack_notices``) as the operator reads it: code, facts, sentence."""
    code = notice.get("code", "")
    params = {k: v for k, v in notice.items() if k != "code" and v is not None}
    return routing_detail(f"notice.{code}", **params) | {"code": code}


async def visible_source(db, data, user):
    archive, library = await item_source(db, data)
    own = Permission.ARCHIVES_READ_OWN if data.archive_id else Permission.LIBRARY_READ_OWN
    all_ = Permission.ARCHIVES_READ_ALL if data.archive_id else Permission.LIBRARY_READ_ALL
    if user is not None and not user.has_any_permission(own.value, all_.value):
        raise HTTPException(403, routing_detail("source_read_forbidden"))
    if data.archive_id:
        from backend.app.api.routes.archives import _ensure_archive_visible

        _ensure_archive_visible(archive, user, user is None or user.has_permission(Permission.ARCHIVES_READ_ALL.value))
    else:
        from backend.app.api.routes.library import _ensure_library_file_visible

        _ensure_library_file_visible(
            library, user, user is None or user.has_permission(Permission.LIBRARY_READ_ALL.value)
        )
    return archive, library


async def printer_routing_preview(db, data, user):
    """Read-only full assignments for actual plate/target pairs, never admission gates.

    Queue sources are authorised by their owning row, not by a caller-provided
    blob id. Its managed bytes remain the source even after the original is gone.
    """
    item = descriptor = saved_queue = None
    archive = library = None
    if data.source_queue_item_id:
        item = await db.get(PrintQueueItem, data.source_queue_item_id)
        if item is None or (
            user is not None
            and not (
                user.has_permission(Permission.QUEUE_READ_ALL.value)
                or (user.has_permission(Permission.QUEUE_READ_OWN.value) and item.created_by_id == user.id)
            )
        ):
            raise HTTPException(404, "Queue item not found")
        descriptor = await item_descriptor(db, item)
        saved_queue = await db.get(PrinterQueue, item.queue_id)
        if descriptor is None:
            archive, library = await visible_source(db, item, user)
    else:
        archive, library = await visible_source(db, data, user)
    cache = PrintRequirementsCache()
    results = []
    async with AsyncExitStack() as stack:
        if descriptor is not None:
            await stack.enter_async_context(queue_sources.pin(descriptor.queue_source_id))
        for target in data.targets:
            req = await cache.read(
                resolve_source_path(archive, library, descriptor=descriptor),
                target.plate_id,
                archive_plate_id=descriptor.plate_fallback if descriptor else archive.plate_index if archive else None,
                sha256=descriptor.sha256 if descriptor else None,
            )
            answer = {
                "printer_id": target.printer_id,
                "plate_id": target.plate_id,
                "status": "unknown",
                "mapping": None,
                "reason": None,
                "channels": [],
                "dual_nozzle": False,
                "direct_rack": False,
                "notices": [],
            }
            printer = await db.get(Printer, target.printer_id)
            if printer is None or printer.archived:
                answer["reason"] = routing_detail("printer_offline")
                results.append(answer)
                continue
            choices = {**data.model_dump(), **target.model_dump()}
            try:
                snapshot = printer_manager.get_feed_snapshot(target.printer_id)
                policy = choices_policy(choices, snapshot)
                # An untouched saved pin is historical evidence, not consent to
                # recapture whatever is in those same numbered trays today.
                if data.editing_queue_item and target.manual_mapping and not target.remap_filament:
                    previous = queue_policy(item)
                    if previous.mode == "pinned":
                        if (
                            saved_queue is None
                            or saved_queue.printer_id != target.printer_id
                            or (item.plate_id or 0) != target.plate_id
                        ):
                            policy = replace(policy, review_required=True)
                        else:
                            policy = replace(
                                policy, physical_pins=previous.physical_pins, review_required=previous.review_required
                            )
                snapshot, prefer_lowest, priority = await ranked_feed(db, target.printer_id, policy)
            except Exception:
                logger.exception("Printer preview telemetry unavailable for %s", target.printer_id)
                answer["reason"] = routing_detail("feed_state_unavailable")
            else:
                saved = decode(item.filament_routing, {}) if data.editing_queue_item else {}
                if not isinstance(saved, dict):
                    saved = {}
                    policy = replace(policy, review_required=True)
                exact_model = (
                    saved.get("exact_model", item.source_auto_item_id is not None) if data.editing_queue_item else False
                )
                if data.editing_queue_item and (
                    saved_queue is None
                    or saved_queue.printer_id != target.printer_id
                    or item.plate_id != target.plate_id
                ):
                    exact_model = model_compatibility(req.model, snapshot.model) == "exact"
                result = resolve_filament_routing(
                    req,
                    policy,
                    snapshot,
                    exact_model=exact_model,
                    prefer_lowest=prefer_lowest,
                    source_priority=priority,
                )
                answer.update(
                    status=result.status,
                    mapping=result.plan.mapping if result.plan else None,
                    reason=routing_detail(result.reason, **result.params) if result.reason else None,
                    channels=explain_channels(req, policy, snapshot, result),
                    dual_nozzle=is_dual_nozzle_model(snapshot.model),
                    # A direct rack (H3C, P3C): one toolhead, never "two nozzles";
                    # the plan's notices (manual feed, two sources on one PrintTool).
                    direct_rack=snapshot.direct_rack,
                    notices=[render_notice(notice) for notice in result.notices],
                )
            results.append(answer)
    return {"targets": results}


async def routing_preview(db, data, user, *, printer_scope: frozenset[int] | None = None):
    """``printer_scope`` (N62): the printers the job's author, the caller, reaches; the
    distributor routes over exactly those (``auto_queue_eligibility.printers_for_item``)."""
    archive, library = await visible_source(db, data, user)
    query = (
        select(Printer, PrinterQueue)
        .join(PrinterQueue, PrinterQueue.printer_id == Printer.id)
        .where(
            Printer.is_active.is_(True),
            Printer.archived.is_(False),
        )
    )
    if printer_scope is not None:
        query = query.where(Printer.id.in_(printer_scope))
    if data.target_location_id:
        tree = await load_tree(db)
        query = query.where(Printer.location_id.in_(subtree_ids(tree, data.target_location_id)))
    printers = (await db.execute(query)).all()
    setting = await db.scalar(select(Settings.value).where(Settings.key == "auto_queue_compatible_models"))
    allow_compatible = isinstance(setting, str) and setting.lower() == "true"
    snapshots = {}
    advisory_unavailable = False
    for printer, _ in printers:
        try:
            snapshots[printer.id] = printer_manager.get_feed_snapshot(printer.id)
        except Exception:
            # Requirements remain usable when live monitoring is unavailable.
            # Dispatch will still require a fresh compatible snapshot.
            logger.exception("Routing preview telemetry unavailable for printer %s", printer.id)
            advisory_unavailable = True
    cache = PrintRequirementsCache()
    policy = choices_policy(data.model_dump())
    plates = []
    for plate in dict.fromkeys(data.plate_ids):
        req = await cache.read(
            resolve_source_path(archive, library), plate, archive_plate_id=archive.plate_index if archive else None
        )
        req = await enrich_family_filament_types(db, req)
        target_model = normalize_model_name(data.target_model) or req.model
        groups = {}
        if req.status == "ok":
            for printer, queue in printers:
                model = printer_manager.effective_model_for(printer.id, printer.model)
                target_verdict = model_compatibility(target_model, model)
                if target_verdict != "exact" and not (allow_compatible and target_verdict == "compatible"):
                    continue
                verdict = model_compatibility(req.model, model)
                if verdict not in ("exact", "compatible"):
                    continue
                snapshot = snapshots.get(printer.id)
                if snapshot is None:
                    continue
                ams = ("present" if snapshot.ams_present else "absent") if snapshot.ams_known else "unknown"
                nozzles = 2 if is_dual_nozzle_model(printer.model) else 1
                key = f"{model}:{nozzles}:{ams}"
                group = groups.setdefault(
                    key,
                    {
                        "key": key,
                        "model": model,
                        "nozzles": nozzles,
                        "ams": ams,
                        "total": 0,
                        "compatible": 0,
                        "unknown": 0,
                        "incompatible": 0,
                        "ready": 0,
                        "reasons": {},
                    },
                )
                result = resolve_filament_routing(req, policy, snapshot, exact_model=verdict == "exact")
                group["total"] += 1
                group[result.status] += 1
                if result.reason:
                    # One line stands for every printer of this model, so it may
                    # carry what the FILE asks for but not what any one printer
                    # has loaded — those trays differ between the machines being
                    # counted together.
                    shared = {k: v for k, v in result.params.items() if k in ("slot", "wanted")}
                    reason = group["reasons"].setdefault(
                        result.reason, {**routing_detail(result.reason, **shared), "count": 0}
                    )
                    reason["count"] += 1
                if (
                    result.plan
                    and queue.auto_distribute_eligible
                    and not queue.is_paused
                    and queue.status != "printing"
                    and not queue.pending_count
                ):
                    from backend.app.services.print_scheduler import scheduler

                    if scheduler._is_printer_ready_for_dispatch(printer.id):
                        group["ready"] += 1
        for group in groups.values():
            group["reasons"] = list(group["reasons"].values())
        plates.append(
            {
                "requested_plate_id": plate,
                "plate_id": req.resolved_plate_id,
                "status": req.status,
                "reason": routing_detail(req.reason) if req.reason else None,
                "model": req.model,
                "target_model": target_model,
                "filaments": list(req.used_filaments),
                "groups": sorted(groups.values(), key=lambda g: g["key"]),
            }
        )
    return {
        "plates": plates,
        "advisory_unavailable": advisory_unavailable,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }
