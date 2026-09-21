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
from backend.app.services.filament_routing import resolve_filament_routing
from backend.app.services.printer_location_service import load_tree, subtree_ids
from backend.app.services.printer_manager import printer_manager
from backend.app.utils.printer_models import is_dual_nozzle_model, normalize_model_name

logger = logging.getLogger(__name__)


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
                )
            results.append(answer)
    return {"targets": results}


async def routing_preview(db, data, user):
    archive, library = await visible_source(db, data, user)
    query = (
        select(Printer, PrinterQueue)
        .join(PrinterQueue, PrinterQueue.printer_id == Printer.id)
        .where(
            Printer.is_active.is_(True),
            Printer.archived.is_(False),
        )
    )
    if data.target_location_id:
        tree = await load_tree(db)
        query = query.where(Printer.location_id.in_(subtree_ids(tree, data.target_location_id)))
    printers = (await db.execute(query)).all()
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
        groups = {}
        if req.status == "ok":
            for printer, queue in printers:
                model = normalize_model_name(printer.model)
                if model != req.model:
                    continue
                snapshot = snapshots.get(printer.id)
                if snapshot is None:
                    continue
                ams = ("present" if snapshot.ams_present else "absent") if snapshot.ams_known else "unknown"
                nozzles = 2 if is_dual_nozzle_model(model) else 1
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
                result = resolve_filament_routing(req, policy, snapshot)
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

                    if scheduler._is_printer_idle(printer.id):
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
                "filaments": list(req.used_filaments),
                "groups": sorted(groups.values(), key=lambda g: g["key"]),
            }
        )
    return {
        "plates": plates,
        "advisory_unavailable": advisory_unavailable,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }
