"""Per-plate hardware compatibility, using the same reader and resolver as dispatch."""

import logging
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select

from backend.app.core.permissions import Permission
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.filament_intake import (
    enrich_family_filament_types,
    item_source,
    resolve_source_path,
    routing_detail,
)
from backend.app.services.filament_policy import choices_policy
from backend.app.services.filament_requirements import PrintRequirementsCache
from backend.app.services.filament_routing import resolve_filament_routing
from backend.app.services.printer_location_service import load_tree, subtree_ids
from backend.app.services.printer_manager import printer_manager
from backend.app.utils.printer_models import is_dual_nozzle_model, normalize_model_name

logger = logging.getLogger(__name__)


async def routing_preview(db, data, user):
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
