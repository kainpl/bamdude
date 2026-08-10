"""GET / POST /printers/{printer_id}/settings — Printer Settings dialog backend.

Mirrors BS PrintOptionsDialog + PrinterPartsDialog. State pulled from
PrinterState, writes routed through bambu_mqtt publishers. Every applied
POST writes a printer_setting_audit row (m061).
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.printer import Printer
from backend.app.models.printer_setting_audit import PrinterSettingAudit
from backend.app.models.user import User
from backend.app.schemas.printer_settings import (
    AddonInfo,
    AiDetectorState,
    CameraSnapshotAction,
    NozzleInfoOut,
    PartsState,
    PrinterSettingsGetResponse,
    PrinterSettingsPostBody,
    PrinterSettingsPostResponse,
    PrinterSettingsSupports,
    PrintOptionBoolAction,
    PrintOptionIntAction,
    PrintOptionsState,
    SafetyIdleHeatingAction,
    SafetyOpenDoorAction,
    SafetyState,
    SetNozzleAction,
    XCamControlAction,
)
from backend.app.services.printer_capabilities import compute_printer_supports
from backend.app.services.printer_manager import is_printer_busy, printer_manager
from backend.app.utils.printer_models import is_dual_nozzle_model

router = APIRouter(prefix="/printers", tags=["printer-settings"])

# Map keys/modules → MQTT method on the client.
_BOOL_KEY_METHODS = {
    "auto_recovery": "print_option_auto_recovery",
    "sound": "print_option_sound",
    "filament_tangle": "print_option_filament_tangle",
    "nozzle_blob": "print_option_nozzle_blob",
    "plate_type": "print_option_plate_type",
    "plate_align": "print_option_plate_align",
    "plate_mark": "print_option_plate_mark",
    "air_print_nonvisual": "print_option_air_print_detect",
}
_INT_KEY_METHODS = {
    "purify_air": "print_option_purify_air",
    "save_remote_to_storage": "print_option_save_remote_to_storage",
    "nozzle_blob_v2": "print_option_nozzle_blob_v2",
}
_BOOL_KEY_SUPPORTS = {
    "auto_recovery": "auto_recovery",
    "sound": "sound",
    "filament_tangle": "filament_tangle",
    "nozzle_blob": "nozzle_blob",
    "plate_type": "plate_type",
    "plate_align": "plate_align",
    "plate_mark": "plate_mark",
    "air_print_nonvisual": "air_print_nonvisual",
}
_INT_KEY_SUPPORTS = {
    "purify_air": "purify_air",
    "save_remote_to_storage": "save_remote_to_storage",
    "nozzle_blob_v2": "smart_nozzle_blob",
}
_XCAM_MODULE_SUPPORTS = {
    "spaghetti_detector": "spaghetti_detector",
    "purgechutepileup_detector": "pileup_detector",
    "nozzleclumping_detector": "nozzleclumping_detector",
    "airprinting_detector": "airprinting_detector",
    "first_layer_inspector": "first_layer_inspector",
    "ai_monitoring": "ai_monitoring_legacy",
    "fod_check": "fod_check",
    "displacement_detection": "displacement_detection",
}


# BS ``AIR_FUN::FAN_CHAMBER_0_IDX``. A part with this id in ``device.airduct.parts``
# is the chamber exhaust fan, and its presence is the whole of BS's
# ``AirDuctData::IsExaustFanExit()``.
_AIRDUCT_PART_CHAMBER_EXHAUST = 3


def _guard_purify_air(is_busy: bool, state, value: int) -> None:
    """The two refusals BS puts on end-of-print air purification.

    ``support_purify_air`` says the machine has the feature; neither of these
    says that, and skipping them is how a request that BS would never have let
    a user make reaches the printer anyway.

    **Not while a job is on it** — BS disables the whole control under
    ``is_in_printing()`` and answers a click with "Unavailable during the task"
    (``PrintOptionsDialog.cpp``, ``m_print_option_disable``). A greyed-out
    checkbox is enough of a guard for a desktop app with one window; ours is an
    HTTP surface an API key can reach mid-print. ``is_busy`` is passed in rather
    than looked up here so this stays a pure function of its inputs.

    **Mode 2 needs a fan to do it with** — purification runs one of two ways,
    and the second exists only where the air duct actually has a chamber
    exhaust part. Without one BS hides the mode switch entirely and the option
    is a plain on/off, so value 2 on such a machine is a mode nothing can
    perform. 0 and 1 stay allowed everywhere.
    """
    if is_busy:
        raise HTTPException(409, "purify_air cannot be changed while a print is running")

    if value == 2 and _AIRDUCT_PART_CHAMBER_EXHAUST not in (getattr(state, "airduct_parts", None) or {}):
        raise HTTPException(409, "purify_air mode 2 requires a chamber exhaust fan")


def _action_tab(action: str) -> str:
    if action in {"print_option_bool", "print_option_int", "xcam_control", "camera_snapshot"}:
        return "print_options"
    if action == "set_nozzle":
        return "parts"
    if action in {"safety_open_door", "safety_idle_heating"}:
        return "safety"
    return "unknown"


# Models that ship a dedicated printer thumbnail under frontend/src/assets/addons.
_PRINTER_THUMB_SLUGS = frozenset(
    {"x2d", "p2s", "h2d", "h2dpro", "h2c", "h2s", "x1c", "x1e", "p1p", "p1s", "a1", "a1mini"}
)


def _model_slug(model: str | None) -> str:
    return (model or "").strip().lower().replace(" ", "").replace("-", "")


def _resolve_addon(name: str, product_name: str, printer_model: str | None) -> tuple[str, str]:
    """Return ``(category, image_key)`` for a get_version module — mirrors BS's
    Update Device row routing (prefix on the module ``name``, substring on
    ``product_name``). ``image_key`` maps 1:1 to a frontend asset; the frontend
    falls back to a generic icon for unknown keys.
    """
    n = (name or "").lower()
    p = product_name or ""
    if n == "ota":  # the printer body
        slug = _model_slug(printer_model)
        return "printer", (f"printer_{slug}" if slug in _PRINTER_THUMB_SLUGS else "printer_generic")
    if n.startswith("ams_f1"):
        return "ams_lite", "ams_lite"
    if n.startswith("n3s"):
        return "ams_ht", "ams_ht"
    if n.startswith(("n3f", "ams")):
        return "ams", "ams"
    if n == "ext":
        return "ext", "ext"
    # Accessories — BS substring rules on product_name (DevFirmware.h isXxx()).
    if "Filament Buffer" in p:
        slug = _model_slug(printer_model)
        return "filament_buffer", (f"filament_buffer_{slug}" if slug in {"x2d", "p2s"} else "filament_buffer_x2d")
    if "Exhaust Fan" in p:
        return "exhaust_fan", "exhaust_fan"
    if "Air Pump" in p:
        return "air_pump", "air_pump"
    if "Laser" in p:
        return "laser", ("laser_40" if "40" in p else "laser")
    if "Cutting Module" in p:
        return "cutting", "cutting"
    if "Extinguishing" in p:
        return "extinguish", "extinguish"
    if "Rotary" in p:
        return "rotary", "rotary"
    if "Filament Track" in p:
        return "filament_track", "filament_track"
    if "wtm" in n:
        return "nozzle_rack", "nozzle_rack"
    return "module", "module"  # generic fallback → frontend renders a lucide icon


def _clean_ver(v: object) -> str | None:
    """Normalise a hw/sw/serial string: empty or ``N/A`` → None."""
    s = str(v or "").strip()
    return None if not s or s.upper() == "N/A" else s


@router.get("/{printer_id}/settings", response_model=PrinterSettingsGetResponse)
async def get_printer_settings(
    printer_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PRINTERS_READ),
) -> PrinterSettingsGetResponse:
    printer = (await db.execute(select(Printer).where(Printer.id == printer_id))).scalar_one_or_none()
    if not printer:
        raise HTTPException(404, "Printer not found")

    client = printer_manager.get_client(printer_id)
    if not client or not client.state.connected:
        raise HTTPException(404, "Printer not online")

    po = client.state.print_options
    state = PrintOptionsState(
        auto_recovery=getattr(po, "auto_recovery_step_loss", None),
        sound=getattr(po, "sound_enable", None),
        filament_tangle=getattr(po, "filament_tangle_detect", None),
        nozzle_blob=getattr(po, "nozzle_blob_detect", None),
        save_remote_to_storage=getattr(po, "save_remote_to_storage", None),
        purify_air=getattr(po, "air_purification", None),
        open_door=getattr(po, "open_door_check", None),
        plate_type=getattr(po, "plate_type_detect", None),
        plate_align=getattr(po, "plate_align_check", None),
        plate_mark=getattr(po, "buildplate_marker_detector", None),
        snapshot=getattr(po, "snapshot_enabled", None),
        fod_check=getattr(po, "fod_check", None),
        displacement_detection=getattr(po, "displacement_detection", None),
        nozzle_blob_v2=getattr(po, "nozzle_blob_v2", None),
        air_print_nonvisual=getattr(po, "air_print_nonvisual", None),
        spaghetti_detector=AiDetectorState(
            enabled=getattr(po, "spaghetti_detector", None),
            sensitivity=getattr(po, "halt_print_sensitivity", None),
        ),
        pileup_detector=AiDetectorState(
            enabled=getattr(po, "pileup_detector", None),
            sensitivity=getattr(po, "pileup_sensitivity", None),
        ),
        nozzleclumping_detector=AiDetectorState(
            enabled=getattr(po, "nozzle_clumping_detector", None),
            sensitivity=getattr(po, "nozzle_clumping_sensitivity", None),
        ),
        airprinting_detector=AiDetectorState(
            enabled=getattr(po, "airprint_detector", None),
            sensitivity=getattr(po, "airprint_sensitivity", None),
        ),
        first_layer_inspector=AiDetectorState(
            enabled=getattr(po, "first_layer_inspector", None),
            sensitivity=None,
        ),
        ai_monitoring=AiDetectorState(
            enabled=getattr(po, "printing_monitor", None),
            sensitivity=getattr(po, "ai_monitoring_sensitivity", None),
        ),
    )

    def _str_or_none(v):
        # NozzleInfo dataclass defaults populate fields as empty strings for
        # printers that don't report them — surface those as None to the API.
        if v is None or v == "":
            return None
        return str(v)

    def _float_or_none(v):
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    # ``state.nozzles`` always holds two default NozzleInfo entries; only
    # dual-nozzle models actually have a second (left) nozzle. Single-nozzle
    # printers must expose just one, otherwise the Parts dialog renders a phantom
    # empty second nozzle (all "—").
    nozzle_count = 2 if is_dual_nozzle_model(printer.model) else 1
    nozzles = []
    for idx, n in enumerate((getattr(client.state, "nozzles", []) or [])[:nozzle_count]):
        nozzles.append(
            NozzleInfoOut(
                id=idx,
                type=_str_or_none(getattr(n, "nozzle_type", None)),
                diameter=_float_or_none(getattr(n, "nozzle_diameter", None)),
                flow_type=_str_or_none(getattr(n, "nozzle_flow", None)),
            )
        )
    parts = PartsState(nozzles=nozzles)

    addons = []
    for mod in getattr(client.state, "modules", []) or []:
        category, image_key = _resolve_addon(getattr(mod, "name", ""), getattr(mod, "product_name", ""), printer.model)
        addons.append(
            AddonInfo(
                name=str(getattr(mod, "name", "") or ""),
                display_name=str(getattr(mod, "product_name", "") or ""),
                category=category,
                image_key=image_key,
                hw_ver=_clean_ver(getattr(mod, "hw_ver", None)),
                sw_ver=_clean_ver(getattr(mod, "sw_ver", None)),
                serial=_clean_ver(getattr(mod, "serial", None)),
            )
        )

    supports = PrinterSettingsSupports(
        **compute_printer_supports(client.state, printer.model, getattr(client, "module_vers", {}))
    )

    safety = SafetyState(
        open_door=getattr(po, "open_door_check", None),
        idle_heating=getattr(po, "idle_heating_protect", None),
    )

    return PrinterSettingsGetResponse(print_options=state, parts=parts, supports=supports, addons=addons, safety=safety)


@router.post("/{printer_id}/settings", response_model=PrinterSettingsPostResponse)
async def post_printer_settings(
    printer_id: int,
    body: PrinterSettingsPostBody = Body(...),
    db: AsyncSession = Depends(get_db),
    user: User | None = RequirePermission(Permission.PRINTERS_UPDATE),
) -> PrinterSettingsPostResponse:
    printer = (await db.execute(select(Printer).where(Printer.id == printer_id))).scalar_one_or_none()
    if not printer:
        raise HTTPException(404, "Printer not found")

    client = printer_manager.get_client(printer_id)
    if not client or not client.state.connected:
        raise HTTPException(404, "Printer not online")

    supports = compute_printer_supports(client.state, printer.model, getattr(client, "module_vers", {}))

    sequence_id: str | None = None
    error: str | None = None
    result_label = "sent"
    ok = True

    try:
        if isinstance(body, PrintOptionBoolAction):
            if not supports.get(_BOOL_KEY_SUPPORTS[body.key]):
                raise HTTPException(409, f"{body.key} not supported on this printer")
            method = getattr(client, _BOOL_KEY_METHODS[body.key])
            ok, sequence_id = method(body.enabled)

        elif isinstance(body, PrintOptionIntAction):
            if not supports.get(_INT_KEY_SUPPORTS[body.key]):
                raise HTTPException(409, f"{body.key} not supported on this printer")
            if body.key == "purify_air":
                _guard_purify_air(is_printer_busy(printer.id), client.state, body.value)
            method = getattr(client, _INT_KEY_METHODS[body.key])
            ok, sequence_id = method(body.value)

        elif isinstance(body, XCamControlAction):
            if not supports.get(_XCAM_MODULE_SUPPORTS[body.module]):
                raise HTTPException(409, f"{body.module} not supported on this printer")
            ok, sequence_id = client.xcam_control_for_settings(
                body.module, enabled=body.enabled, sensitivity=body.sensitivity
            )

        elif isinstance(body, CameraSnapshotAction):
            if not supports.get("snapshot"):
                raise HTTPException(409, "snapshot not supported on this printer")
            ok, sequence_id = client.camera_snapshot_enable(body.enabled)

        elif isinstance(body, SafetyOpenDoorAction):
            # Open-door detection: valid in Print Options (non-safety models) or the
            # Safety tab (X2D/P2S). Same MQTT command (system/set_door_stat) for both.
            if not (supports.get("open_door_check") or supports.get("safety_tab")):
                raise HTTPException(409, "open_door not supported on this printer")
            ok, sequence_id = client.set_door_open_check(body.value)

        elif isinstance(body, SafetyIdleHeatingAction):
            if not supports.get("idle_heating"):
                raise HTTPException(409, "idle_heating not supported on this printer")
            ok, sequence_id = client.set_idle_heating(body.enabled)

        elif isinstance(body, SetNozzleAction):
            raise HTTPException(409, "parts_not_editable")

        else:
            raise HTTPException(400, "unknown action")

        if not ok:
            result_label = "error"
            error = "MQTT publish failed"

    except HTTPException:
        raise
    except Exception as exc:
        result_label = "error"
        error = str(exc)

    db.add(
        PrinterSettingAudit(
            printer_id=printer_id,
            user_id=user.id if user else None,
            tab=_action_tab(body.action),
            action=body.action,
            payload_json=json.dumps(body.model_dump(mode="json")),
            sequence_id=sequence_id,
            result=result_label,
            error_message=error,
        )
    )
    await db.commit()

    if result_label == "error":
        raise HTTPException(504, error or "MQTT publish failed")

    return PrinterSettingsPostResponse(ok=True, sequence_id=sequence_id)
