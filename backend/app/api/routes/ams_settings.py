"""GET / POST /printers/{printer_id}/ams/settings — AMS Settings dialog backend.

Mirrors BambuStudio's AMSSetting dialog (see spec
``docs/superpowers/specs/2026-05-12-ams-settings-dialog-design.md``). Reads
state from in-memory ``PrinterState``, writes via ``BambuMQTTClient``
publishers, and records every applied POST in ``ams_setting_audit``.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.ams_setting_audit import AmsSettingAudit
from backend.app.models.printer import Printer
from backend.app.models.user import User
from backend.app.schemas.ams_settings import (
    AmsAirPrintAction,
    AmsAutoSwitchAction,
    AmsCalibrateAction,
    AmsFirmwareOption,
    AmsFirmwareSwitchAction,
    AmsReorderAction,
    AmsSettingsGetResponse,
    AmsSettingsPostBody,
    AmsSettingsPostResponse,
    AmsSystemSettingState,
    AmsSystemSettingSupports,
    AmsUnitInfo,
    AmsUserSettingAction,
)
from backend.app.services.ams_capabilities import compute_ams_supports
from backend.app.services.printer_manager import printer_manager

router = APIRouter(prefix="/printers", tags=["ams-settings"])


@router.get("/{printer_id}/ams/settings/debug")
async def debug_ams_state(
    printer_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PRINTERS_UPDATE),
):
    """Diagnostic dump — what we actually received from the printer.

    Returns the live raw_data top-level keys, any field that smells AMS-related,
    and the decoded state. Use this when a checkbox doesn't reflect the real
    printer setting so we can see whether the printer is sending the data at
    all and where it's hiding.
    """
    printer = (await db.execute(select(Printer).where(Printer.id == printer_id))).scalar_one_or_none()
    if not printer:
        raise HTTPException(404, "Printer not found")

    client = printer_manager.get_client(printer_id)
    if not client:
        raise HTTPException(404, "Printer not online")

    raw = client.state.raw_data or {}

    def _is_relevant_key(k: str) -> bool:
        kl = k.lower()
        return any(
            tok in kl
            for tok in (
                "cfg",
                "flag",
                "fun",
                "ams",
                "insert",
                "power_on",
                "remain",
                "switch",
                "backup",
                "refill",
                "air_print",
            )
        )

    relevant_top = {k: v for k, v in raw.items() if _is_relevant_key(k)}

    # Also dump any *string* fields at top level — cfg is a hex string in BS
    # newer firmware and easy to spot.
    string_top = {k: v for k, v in raw.items() if isinstance(v, str) and 2 <= len(v) <= 32}

    ams_subkey = raw.get("ams") if isinstance(raw.get("ams"), dict) else None
    ams_scalar_fields = {}
    if ams_subkey:
        ams_scalar_fields = {
            k: v for k, v in ams_subkey.items() if not isinstance(v, (list, dict)) and _is_relevant_key(k)
        }

    return {
        "printer_model": printer.model,
        "connected": client.state.connected,
        "decoded": {
            "ams_insertion_update": client.state.ams_insertion_update,
            "ams_power_on_update": client.state.ams_power_on_update,
            "ams_remain_capacity": client.state.ams_remain_capacity,
            "ams_auto_switch_filament": client.state.ams_auto_switch_filament,
            "ams_air_print_detect": client.state.ams_air_print_detect,
        },
        "raw_top_keys": sorted(raw.keys()),
        "relevant_top_fields": relevant_top,
        "string_top_fields": string_top,
        "ams_subkey_scalar_fields": ams_scalar_fields,
    }


def _ams_label(ams_id: int) -> str:
    """Mirror frontend's amsHelpers.formatSlotLabel for the unit selector."""
    if ams_id == 255:
        return "External"
    if ams_id >= 128:
        return f"HT-{chr(ord('A') + (ams_id - 128))}"
    return f"AMS {chr(ord('A') + ams_id)}"


@router.get("/{printer_id}/ams/settings", response_model=AmsSettingsGetResponse)
async def get_ams_settings(
    printer_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PRINTERS_READ),
) -> AmsSettingsGetResponse:
    printer = (await db.execute(select(Printer).where(Printer.id == printer_id))).scalar_one_or_none()
    if not printer:
        raise HTTPException(404, "Printer not found")

    client = printer_manager.get_client(printer_id)
    if not client or not client.state.connected:
        raise HTTPException(404, "Printer not online")

    s = client.state
    state = AmsSystemSettingState(
        insertion_update=s.ams_insertion_update,
        power_on_update=s.ams_power_on_update,
        remain_capacity=s.ams_remain_capacity,
        auto_switch_filament=s.ams_auto_switch_filament,
        air_print_detect=s.ams_air_print_detect,
        firmware_idx_run=s.ams_firmware_idx_run,
        firmware_idx_sel=s.ams_firmware_idx_sel,
    )
    supports_dict = compute_ams_supports(s, printer.model)
    supports = AmsSystemSettingSupports(**supports_dict)

    # AMS units for the Calibrate-dropdown. Source: raw_data["ams"]["ams"] list
    # (list of dicts with "id" key as string) — same source the existing AMS
    # panel uses.
    ams_units: list[AmsUnitInfo] = []
    raw_ams = (s.raw_data or {}).get("ams")
    if isinstance(raw_ams, dict):
        ams_list = raw_ams.get("ams") or []
    elif isinstance(raw_ams, list):
        ams_list = raw_ams
    else:
        ams_list = []
    for entry in ams_list:
        if not isinstance(entry, dict):
            continue
        raw_id = entry.get("id")
        try:
            ams_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        ams_units.append(AmsUnitInfo(ams_id=ams_id, label=_ams_label(ams_id)))

    firmware_options: list[AmsFirmwareOption] = []
    if supports.firmware_switch:
        # BS DevAmsSystemFirmwareSwitch::IDX_AMS = 0 (FULL), IDX_LITE = 1.
        firmware_options = [
            AmsFirmwareOption(idx=0, label="FULL"),
            AmsFirmwareOption(idx=1, label="LITE"),
        ]

    return AmsSettingsGetResponse(
        state=state,
        supports=supports,
        ams_units=ams_units,
        firmware_options=firmware_options,
    )


@router.post("/{printer_id}/ams/settings", response_model=AmsSettingsPostResponse)
async def post_ams_settings(
    printer_id: int,
    body: AmsSettingsPostBody = Body(...),
    db: AsyncSession = Depends(get_db),
    user: User | None = RequirePermission(Permission.PRINTERS_UPDATE),
) -> AmsSettingsPostResponse:
    printer = (await db.execute(select(Printer).where(Printer.id == printer_id))).scalar_one_or_none()
    if not printer:
        raise HTTPException(404, "Printer not found")

    client = printer_manager.get_client(printer_id)
    if not client or not client.state.connected:
        raise HTTPException(404, "Printer not online")

    supports = compute_ams_supports(client.state, printer.model)

    sequence_id: str | None = None
    error: str | None = None
    result_label = "sent"
    http_exc: HTTPException | None = None

    try:
        if isinstance(body, AmsUserSettingAction):
            # user_setting sends all three flags in one shot. We allow it if
            # any of the three constituent capabilities is supported (BS sends
            # the same triplet regardless of which checkbox was clicked).
            if not (supports["insertion_update"] or supports["power_on_update"] or supports["remain_capacity"]):
                http_exc = HTTPException(409, "user_setting not supported on this printer")
                raise http_exc
            ok, sequence_id = client.ams_user_setting(
                startup_read=body.startup_read_option,
                tray_read=body.tray_read_option,
                calibrate_remain=body.calibrate_remain_flag,
            )

        elif isinstance(body, AmsAutoSwitchAction):
            if not supports["auto_switch_filament"]:
                http_exc = HTTPException(409, "filament backup not supported on this printer")
                raise http_exc
            ok, sequence_id = client.print_option_auto_switch_filament(body.enabled)

        elif isinstance(body, AmsAirPrintAction):
            if not supports["air_print_detect"]:
                http_exc = HTTPException(409, "air print detection not in AMS Settings on this printer")
                raise http_exc
            ok, sequence_id = client.print_option_air_print_detect(body.enabled)

        elif isinstance(body, AmsCalibrateAction):
            ok = client.ams_calibrate(body.ams_id)

        elif isinstance(body, AmsFirmwareSwitchAction):
            if not supports["firmware_switch"]:
                http_exc = HTTPException(409, "firmware switch not supported on this printer")
                raise http_exc
            ok, sequence_id = client.ams_firmware_switch(body.firmware_idx)

        elif isinstance(body, AmsReorderAction):
            if not supports["reorder"]:
                http_exc = HTTPException(409, "AMS reorder not supported on this printer")
                raise http_exc
            ok, sequence_id = client.ams_reset_sequence()

        else:
            http_exc = HTTPException(400, "unknown action")
            raise http_exc

        if not ok:
            result_label = "error"
            error = "MQTT publish failed"
    except HTTPException:
        # Re-raised below after audit; but for 409/400 we DON'T write audit —
        # nothing was sent. Just propagate without recording.
        raise
    except Exception as exc:
        result_label = "error"
        error = str(exc)

    # Audit (success or send-error). Skip for HTTPException (handled above).
    db.add(
        AmsSettingAudit(
            printer_id=printer_id,
            user_id=user.id if user else None,
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

    return AmsSettingsPostResponse(ok=True, sequence_id=sequence_id)
