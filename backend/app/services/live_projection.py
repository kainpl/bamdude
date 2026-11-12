"""Explicit, scalar WebSocket projections, shared by every delivery path.

Preparation asks the live coordinator for a coalesced metadata read, never a
database session in a producer or a query per viewer. Shapers are pure and
their results may be serialized once per current entitlement class.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field

from backend.app.services.live_authority import Entitlements, live_authority

CONTROL_EVENTS = frozenset({"pong", "initial_status_complete", "farm_startup_changed"})
PRINT_EVENTS = frozenset({"print_start", "print_complete", "print_paused", "print_resumed"})
CALIBRATION_EVENTS = frozenset(
    f"calibration.{name}" for name in ("started", "saved", "cancelled", "failed", "completed")
)
ZIGBEE_EVENTS = frozenset(
    {
        "zigbee_device_joining",
        "zigbee_device_paired",
        "zigbee_device_rejected",
        "zigbee_device_left",
        "zigbee_status_changed",
    }
)
INVENTORY_EVENTS = frozenset(
    {
        "spool_assignment_changed",
        "spool_assignment_verified",
        "spool_auto_assigned",
        "spool_usage_logged",
        "stock_spool_config_failed",
        "stock_spool_unavailable",
        "unknown_tag",
        "missing_spool_assignment",
        "filament_deficit",
    }
)
KNOWN_EVENTS = (
    CONTROL_EVENTS
    | PRINT_EVENTS
    | CALIBRATION_EVENTS
    | ZIGBEE_EVENTS
    | INVENTORY_EVENTS
    | frozenset(
        {
            "printer_status",
            "archive_created",
            "archive_updated",
            "library_file_added",
            "library_file_notes_changed",
            "library_scan_progress",
            "library_scan_finished",
            "queue_changed",
            "stagger_changed",
            "background_dispatch",
            "plate_not_empty",
            "inventory_changed",
            "firmware_upload_progress",
            "firmware_batch_progress",
            "macro_executed",
            "telegram_chat_registered",
            "sensor_bindings_changed",
            "part_images_changed",
            "inbox_item",
        }
    )
)
# Deliberately finite: a future producer field needs a projection decision.
STATUS_FIELDS = frozenset(
    [
        "connected",
        "mqtt_recording",
        "mqtt_recording_bytes",
        "effective_model",
        "state",
        "progress",
        "remaining_time",
        "layer_num",
        "total_layers",
        "total_layers_source",
        "temperatures",
        "hms_errors",
        "hms_muted",
        "pause_reason",
        "pause_reason_label",
        "pause_started_at",
        "ams",
        "vt_tray",
        "ams_status_main",
        "ams_status_sub",
        "tray_now",
        "expected_tray",
        "previous_tray",
        "ams_auto_switch_filament",
        "ams_backup_groups",
        "ams_extruder_map",
        "fila_switch",
        "fila_switch_pending_confirmation",
        "ams_switch_inlet",
        "extruder_slots",
        "ams_preload_version",
        "wifi_signal",
        "wired_network",
        "stg_cur",
        "stg_cur_name",
        "printable_objects_count",
        "skip_objects_supported",
        "cooling_fan_speed",
        "big_fan1_speed",
        "big_fan2_speed",
        "heatbreak_fan_speed",
        "chamber_light",
        "has_chamber_light",
        "firmware_consistency_request",
        "firmware_force_upgrade",
        "speed_level",
        "door_open",
        "sdcard",
        "sdcard_state",
        "storage_capability",
        "store_to_sdcard",
        "timelapse",
        "ipcam",
        "firmware_version",
        "stg",
        "mc_print_sub_stage",
        "last_ams_update",
        "temperature_limits",
        "ext_has_nozzle",
        "supports_chamber_heater",
        "axis_at_home",
        "is_bed_slinger",
        "ext_has_filament",
        "timelapse_capability",
        "airduct_fans",
        "airduct_mode",
        "airduct_sub_mode",
        "active_extruder",
        "nozzle_rack",
        "supports_drying",
        "supports_drying_while_printing",
        "drying_screen_only",
        "current_plate_id",
        "awaiting_plate_clear",
        "repeat_available",
    ]
)


@dataclass
class PreparedEvent:
    message: dict
    rows: dict = field(default_factory=dict)
    run_archive: int | None = None
    run_identity: str | None = None
    source_printer_identities: Mapping[int, str] = field(default_factory=dict)


class IncarnationChanged(Exception):
    """A restricted viewer must bootstrap rather than inherit a reused ID."""


def _id(value):
    return value if type(value) is int and value > 0 else None


async def prepare(message: dict, *, source_printer_identities=None) -> PreparedEvent:
    """Freeze an event and resolve all its addressed sources in one batch."""
    event = PreparedEvent(deepcopy(message))
    from backend.app.services.printer_manager import printer_manager

    event.source_printer_identities = (
        printer_manager.record_identity_snapshot() if source_printer_identities is None else source_printer_identities
    )
    kind = message.get("type")
    data = message.get("data") or {}
    refs = set()
    if kind in {"archive_created", "archive_updated"}:
        ident = _id(data.get("id") or data.get("archive_id"))
        if ident:
            refs.add(("archive", ident))
    elif kind in {"library_file_added", "library_file_notes_changed"}:
        ident = _id(data.get("id") or data.get("file_id"))
        if ident:
            refs.add(("library", ident))
    if kind == "printer_status" or kind in PRINT_EVENTS:
        from backend.app.services.print_run_binding import current_print_run
        from backend.app.services.printer_manager import printer_manager

        printer_id = _id(message.get("printer_id"))
        run = current_print_run(printer_manager, printer_id) if printer_id else None
        if run is not None:
            event.run_archive, event.run_identity = run.archive_id, run.archive_identity
            refs.add(("archive", run.archive_id))
        ident = _id(data.get("archive_id"))
        if ident:
            refs.add(("archive", ident))
    if kind == "background_dispatch":
        for row in [
            *(data.get("active_jobs") or []),
            *(data.get("dispatched_jobs") or []),
            data.get("recent_event") or {},
        ]:
            source = _id(row.get("source_id"))
            family = {"print_library_file": "library", "reprint_archive": "archive"}.get(row.get("kind"))
            if family and source:
                refs.add((family, source))
    if refs:
        event.rows = await live_authority.resource_rows(refs)
    return event


def _any(access, *permissions):
    return any(access.allows(permission) for permission in permissions)


def _target_visible(access, printer_id, source_identities, credential_kind):
    if not access.printer_visible(printer_id):
        return False
    if credential_kind == "key" and access.printers is not None:
        expected = dict(access.printer_identities).get(printer_id)
        actual = source_identities.get(printer_id) if source_identities is not None else None
        if expected is None or actual != expected:
            raise IncarnationChanged
    return True


def _owns(access, family, row, credential_kind="session", source_identities=None):
    if not row or row.get("deleted_at") is not None:
        return False
    domain = "archives" if family == "archive" else "library"
    if family == "archive" and not _target_visible(access, row.get("printer_id"), source_identities, credential_kind):
        return False
    return access.allows(f"{domain}:read_all") or (
        credential_kind != "key"
        and access.allows(f"{domain}:read_own")
        and row.get("created_by_id") is not None
        and row.get("created_by_id") == access.user_id
    )


def _workshop(access, value):
    """Copy with independent domain masks; never mutate a broadcast's value."""
    hidden = set()
    if not access.allows("orders:read"):
        hidden.update({"project_id", "project_name", "project_line_id", "order_id", "order_name"})
    if not access.allows("products:read"):
        hidden.update({"product_id", "product_name", "part_id", "part_name"})
    if not _any(access, "customers:read", "stock:move"):
        hidden.update({"customer_id", "customer_name", "recipient", "recipient_name", "address", "phone", "email"})
    if not access.allows("stock:read"):
        hidden.update({"stock_position_id", "stock_quantity", "stock_issue_id"})
    if isinstance(value, dict):
        return {k: _workshop(access, v) for k, v in value.items() if k not in hidden}
    if isinstance(value, list):
        return [_workshop(access, item) for item in value]
    return value


def shape(event: PreparedEvent, access: Entitlements, *, personal=False, credential_kind="session"):
    message = event.message
    kind = message.get("type")
    if kind not in KNOWN_EVENTS:
        return None
    data = message.get("data") or {}
    pid = message.get("printer_id", data.get("printer_id"))
    if pid is not None and not _target_visible(access, pid, event.source_printer_identities, credential_kind):
        return None
    if kind in CONTROL_EVENTS:
        if kind == "pong":
            return {"type": kind}
        if kind == "initial_status_complete":
            return {k: message[k] for k in ("type", "bootstrap_id", "printers") if k in message}
        return {"type": kind}  # clients refetch the guarded startup status
    if kind == "inbox_item":
        return deepcopy(message) if personal and access.allows("notifications:inbox") else None
    if kind == "printer_status":
        if not access.allows("printers:read"):
            return None
        result = {k: deepcopy(v) for k, v in data.items() if k in STATUS_FIELDS}
        row = event.rows.get(("archive", event.run_archive))
        visible = bool(
            row
            and event.run_identity
            and row.get("recovery_identity") == event.run_identity
            and _owns(access, "archive", row, credential_kind, event.source_printer_identities)
        )
        for key in ("current_print", "subtask_name", "gcode_file", "cover_url"):
            result[key] = None
        if visible:
            # A delayed payload cannot borrow a newer run's ownership to
            # disclose its predecessor's private names.
            result["current_print"] = row.get("print_name") or row.get("filename")
            result["subtask_name"] = result["current_print"]
            result["gcode_file"] = row.get("filename")
            if access.allows("camera:view"):
                result["cover_url"] = f"/api/v1/printers/{pid}/camera-cover" if data.get("cover_url") else None
        if not access.allows("inventory:read"):
            for unit in result.get("ams", []):
                for tray in unit.get("tray", []):
                    for key in ("tag_uid", "tray_uuid", "actual"):
                        tray.pop(key, None)
            for tray in result.get("vt_tray", []):
                for key in ("tag_uid", "tray_uuid", "actual"):
                    tray.pop(key, None)
        return {"type": kind, "printer_id": pid, "data": _workshop(access, result)}
    if kind in PRINT_EVENTS:
        if not access.allows("printers:read"):
            return None
        result = {
            k: data[k]
            for k in ("status", "reason_code", "paused_for_seconds", "print_time_seconds", "filament_used_grams")
            if k in data
        }
        # Only a writer-stamped binding proves which current row this names.
        row = event.rows.get(("archive", event.run_archive))
        if (
            row
            and event.run_identity
            and row.get("recovery_identity") == event.run_identity
            and _owns(access, "archive", row, credential_kind, event.source_printer_identities)
        ):
            result["filename"] = row.get("print_name") or row.get("filename")
            if data.get("archive_id") == row["id"]:
                result["archive_id"] = row["id"]
            if "hms_code" in data:
                result["hms_code"] = data["hms_code"]
        return {"type": kind, "printer_id": pid, "data": result}
    if kind in {"archive_created", "archive_updated", "library_file_added", "library_file_notes_changed"}:
        family = "archive" if kind.startswith("archive_") else "library"
        ident = _id(data.get("id") or data.get("archive_id") or data.get("file_id"))
        row = event.rows.get((family, ident))
        if _owns(access, family, row, credential_kind, event.source_printer_identities):
            # Publish a current authorized invalidation, never an old payload
            # whose numeric id may have been recycled before preparation.
            out = {"id" if family == "archive" or kind == "library_file_added" else "file_id": ident}
            return {"type": kind, "data": out}
        if family == "archive" and access.allows("orders:read"):
            return {"type": kind, "data": {"restricted": True}}
        return None
    if kind in {"queue_changed", "stagger_changed"}:
        if not _any(access, "queue:read_all", "queue:read_own", "queue:read"):
            return None
        return {"type": kind, **({"printer_id": pid} if pid is not None else {})}
    if kind == "background_dispatch":
        if not _any(access, "queue:read_all", "queue:read_own", "queue:read"):
            return None

        def job(raw):
            if not raw or not _target_visible(
                access, raw.get("printer_id"), event.source_printer_identities, credential_kind
            ):
                return None
            out = {
                k: raw[k]
                for k in (
                    "job_id",
                    "kind",
                    "printer_id",
                    "printer_name",
                    "phase",
                    "upload_bytes",
                    "upload_total_bytes",
                    "upload_progress_pct",
                    "status",
                )
                if k in raw
            }
            family = {"print_library_file": "library", "reprint_archive": "archive"}.get(raw.get("kind"))
            row = event.rows.get((family, raw.get("source_id")))
            if family and _owns(access, family, row, credential_kind, event.source_printer_identities):
                out["source_id"] = row["id"]
                out["source_name"] = row.get("print_name") or row.get("filename")
            return out

        active = [out for raw in data.get("active_jobs", []) if (out := job(raw)) is not None]
        queued = [out for raw in data.get("dispatched_jobs", []) if (out := job(raw)) is not None]
        out = {
            "active_jobs": active,
            "active_job": active[0] if active else None,
            "dispatched_jobs": queued,
            "processing": len(active),
            "dispatched": len(queued),
            "recent_event": job(data.get("recent_event")),
        }
        # Historical totals are only meaningful for a full-farm full-source reader.
        if (
            access.printers is None
            and access.allows("queue:read_all")
            and access.allows("archives:read_all")
            and access.allows("library:read_all")
        ):
            out.update({k: data[k] for k in ("total", "completed", "failed") if k in data})
        return {"type": kind, "data": out}
    if kind == "part_images_changed":
        return deepcopy(message) if _any(access, "products:read", "orders:read", "stock:read") else None
    if kind == "inventory_changed":
        return {"type": kind} if _any(access, "inventory:read", "stock:read") else None
    if kind in INVENTORY_EVENTS:
        if not access.allows("inventory:read"):
            return None
        # Existing finite events; no private print name merely from inventory.
        allowed = {
            "type",
            "printer_id",
            "printer_name",
            "ams_id",
            "tray_id",
            "slot",
            "verified",
            "kprofile_applied",
            "saw_tray",
            "missing_slots",
            "shortfalls",
            "tag_uid",
            "tray_uuid",
            "tray_type",
            "tray_color",
            "tray_sub_brands",
            "tray_count",
            "spool_id",
        }
        out = {k: deepcopy(v) for k, v in message.items() if k in allowed}
        if kind == "spool_usage_logged":
            out["usage"] = [
                {
                    k: row[k]
                    for k in (
                        "spool_id",
                        "weight_used",
                        "percent_used",
                        "ams_id",
                        "tray_id",
                        "material",
                        "cost",
                        "slot_id",
                        "color",
                    )
                    if k in row
                }
                for row in message.get("usage", [])
            ]
        return _workshop(access, out)
    if kind in CALIBRATION_EVENTS:
        return deepcopy(message) if access.allows("printers:read") else None
    if kind in ZIGBEE_EVENTS:
        return deepcopy(message) if access.allows("smart_plugs:read") and access.printers is None else None
    if kind == "sensor_bindings_changed":
        return {"type": kind} if access.allows("smart_sensors:read") else None
    if kind == "telegram_chat_registered":
        return deepcopy(message) if access.allows("notifications:read") else None
    if kind in {"library_scan_progress", "library_scan_finished"}:
        return (
            {
                "type": kind,
                "data": {
                    k: data[k]
                    for k in (
                        "job_id",
                        "folder_id",
                        "status",
                        "total",
                        "files_seen",
                        "files_added",
                        "files_updated",
                        "files_removed",
                        "folders_added",
                        "folders_removed",
                        "skipped_deletions",
                        "warnings",
                    )
                    if k in data
                },
            }
            if access.allows("library:read_all")
            else None
        )
    if kind in {"firmware_upload_progress", "firmware_batch_progress"}:
        if not access.allows("firmware:read"):
            return None
        # Both current producers address one printer, including batch progress.
        # Arbitrary driver messages/errors do not belong in a live projection.
        return {
            k: deepcopy(message[k])
            for k in ("type", "printer_id", "run_id", "status", "progress", "percent", "firmware_version")
            if k in message
        }
    if kind == "macro_executed":
        if not access.allows("printers:read"):
            return None
        return (
            deepcopy(message)
            if access.allows("settings:read")
            else {
                "type": kind,
                "data": {k: data[k] for k in ("printer_id", "printer_name", "status", "success") if k in data},
            }
        )
    if kind == "plate_not_empty":
        return {"type": kind, "printer_id": pid} if access.allows("printers:read") else None
    return None
