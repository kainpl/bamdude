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
    SetNozzleAction,
    XCamControlAction,
)
from backend.app.services.printer_capabilities import compute_printer_supports
from backend.app.services.printer_manager import printer_manager

router = APIRouter(prefix="/printers", tags=["printer-settings"])

# Map keys/modules → MQTT method on the client.
_BOOL_KEY_METHODS = {
    "auto_recovery": "print_option_auto_recovery",
    "sound": "print_option_sound",
    "filament_tangle": "print_option_filament_tangle",
    "nozzle_blob": "print_option_nozzle_blob",
    "plate_type": "print_option_plate_type",
    "plate_align": "print_option_plate_align",
}
_INT_KEY_METHODS = {
    "purify_air": "print_option_purify_air",
    "open_door": "print_option_open_door",
    "save_remote_to_storage": "print_option_save_remote_to_storage",
}
_BOOL_KEY_SUPPORTS = {
    "auto_recovery": "auto_recovery",
    "sound": "sound",
    "filament_tangle": "filament_tangle",
    "nozzle_blob": "nozzle_blob",
    "plate_type": "plate_type",
    "plate_align": "plate_align",
}
_INT_KEY_SUPPORTS = {
    "purify_air": "purify_air",
    "open_door": "open_door_check",
    "save_remote_to_storage": "save_remote_to_storage",
}
_XCAM_MODULE_SUPPORTS = {
    "spaghetti_detector": "spaghetti_detector",
    "purgechutepileup_detector": "pileup_detector",
    "nozzleclumping_detector": "nozzleclumping_detector",
    "airprinting_detector": "airprinting_detector",
    "first_layer_inspector": "first_layer_inspector",
    "ai_monitoring": "ai_monitoring",
    "fod_check": "fod_check",
    "displacement_detection": "displacement_detection",
}


def _action_tab(action: str) -> str:
    if action in {"print_option_bool", "print_option_int", "xcam_control", "camera_snapshot"}:
        return "print_options"
    if action == "set_nozzle":
        return "parts"
    return "unknown"


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
        snapshot=getattr(po, "snapshot_enabled", None),
        fod_check=getattr(po, "fod_check", None),
        displacement_detection=getattr(po, "displacement_detection", None),
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
            sensitivity=None,
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

    nozzles = []
    for idx, n in enumerate(getattr(client.state, "nozzles", []) or []):
        nozzles.append(
            NozzleInfoOut(
                id=idx,
                type=_str_or_none(getattr(n, "nozzle_type", None)),
                diameter=_float_or_none(getattr(n, "nozzle_diameter", None)),
                flow_type=_str_or_none(getattr(n, "nozzle_flow", None)),
            )
        )
    parts = PartsState(nozzles=nozzles)

    supports = PrinterSettingsSupports(
        **compute_printer_supports(client.state, printer.model, getattr(client, "module_vers", {}))
    )

    return PrinterSettingsGetResponse(print_options=state, parts=parts, supports=supports)


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
