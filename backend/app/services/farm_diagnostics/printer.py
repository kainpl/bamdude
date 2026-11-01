"""One visible printer, persisted plug cache and existing camera metrics only."""

from datetime import datetime, timezone

from sqlalchemy import select

from backend.app.models.printer import Printer
from backend.app.models.smart_plug import SmartPlug
from backend.app.services.farm_diagnostics.access import DiagnosticError, printer_scope, require
from backend.app.services.farm_diagnostics.health import safe_health
from backend.app.services.farm_diagnostics.overview import _live_identity, _queue_identity, read_printers
from backend.app.services.queue_sources import current_epoch


async def inspect_printer(db, access, args, request_id):
    require(access, "bamdude_inspect_printer")
    started = datetime.now(timezone.utc)
    pid = await db.scalar(
        select(Printer.id).where(
            Printer.id == args.printer_id, Printer.archived.is_(False), printer_scope(access, Printer.id)
        )
    )
    if pid is None:
        raise DiagnosticError("not_found")
    live_before = _live_identity([pid])
    queue_before = await _queue_identity(db, [pid]) if access.permits("queue:read_all") else None
    result = await read_printers(db, access, [pid], started, request_id)
    if not result.data["printers"]:
        raise DiagnosticError("state_changed")
    admission_allowed = result.data["new_work_admission_allowed"]
    result.data = result.data["printers"][0]
    result.data["new_work_admission_allowed"] = admission_allowed
    result.data["camera"] = {"availability": "unavailable_for_actor"}
    if access.permits("camera:view"):
        from backend.app.services.camera_metrics import for_printer
        from backend.app.services.camera_runtime import camera_runtime_health

        try:
            metrics = for_printer(pid)
            result.data["camera"] = {
                "availability": "available",
                "worker": safe_health(camera_runtime_health()).model_dump(mode="json"),
                "captured_at": None,
                "freshness": "unknown",
                "coverage": {
                    "complete": False,
                    "missing": ["capture_timestamp_not_recorded", "telegram_delivery_history_not_recorded"],
                },
                "stream_frame_age_ms": metrics.get("frame_age_ms") if metrics else None,
                "recent_capture_failed": bool(metrics and metrics.get("consecutive_failures", 0) > 0)
                if metrics
                else None,
            }
        except Exception:
            result.data["camera"] = {"availability": "source_unavailable", "captured_at": None, "freshness": "unknown"}
    result.data["plugs"] = {"availability": "unavailable_for_actor"}
    if access.permits("smart_plugs:read"):
        # Columns, not SmartPlug ORM objects: credentials/URLs are never loaded.
        rows = (
            (
                await db.execute(
                    select(
                        SmartPlug.id,
                        SmartPlug.plug_type,
                        SmartPlug.enabled,
                        SmartPlug.schedule_enabled,
                        SmartPlug.schedule_on_time,
                        SmartPlug.schedule_off_time,
                        SmartPlug.last_state,
                        SmartPlug.last_checked,
                    )
                    .where(SmartPlug.printer_id == pid)
                    .order_by(SmartPlug.id)
                    .limit(101)
                )
            )
            .mappings()
            .all()
        )
        result.data["plugs"] = {
            "availability": "available",
            "items": [
                {
                    "id": row["id"],
                    "type": row["plug_type"]
                    if row["plug_type"] in {"tasmota", "homeassistant", "mqtt", "rest", "zigbee"}
                    else "unknown",
                    "enabled": row["enabled"],
                    "schedule": {
                        "enabled": row["schedule_enabled"],
                        "on": row["schedule_on_time"],
                        "off": row["schedule_off_time"],
                        "time_basis": "server_local_wall_clock",
                        "days": "every_day",
                    },
                    "cached_state": row["last_state"] if row["last_state"] in {"ON", "OFF"} else None,
                    "observed_at": (
                        row["last_checked"].replace(tzinfo=timezone.utc)
                        if row["last_checked"] and row["last_checked"].tzinfo is None
                        else row["last_checked"]
                    ).isoformat()
                    if row["last_checked"]
                    else None,
                    "freshness": "unknown",
                }
                for row in rows[:100]
            ],
            "coverage": {
                "complete": len(rows) <= 100,
                "truncated": len(rows) > 100,
                "missing": ["past_schedule_decisions_not_recorded", "plug_cache_freshness_policy_not_available"],
            },
        }
    if queue_before is not None and await _queue_identity(db, [pid]) != queue_before:
        raise DiagnosticError("state_changed")
    if access.epoch != current_epoch() or _live_identity([pid]) != live_before:
        raise DiagnosticError("state_changed")
    result.collection_finished_at = datetime.now(timezone.utc)
    for name in ("camera", "plugs"):
        section = result.data[name]
        if section["availability"] == "source_unavailable":
            result.coverage.missing.append(name + "_source_unavailable")
        elif section["availability"] == "available":
            result.coverage.missing.extend(section.get("coverage", {}).get("missing", []))
    if result.coverage.missing:
        result.coverage.complete = False
    return result
