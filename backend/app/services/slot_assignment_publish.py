"""The ONE publisher of a SlotAssignmentPlan to an AMS slot.

Every inventory assignment path (internal assign + replay, Spoolman assign,
Spoolman link) publishes through here, so a projected plan
(services/ams_backup_compatibility) reaches the wire from exactly one place and
the read-back verification always expects what was actually sent. The manual
``configure_ams_slot`` route keeps its own order (verification after cali_idx)
and is deliberately not a caller.
"""

from __future__ import annotations

from backend.app.services.slot_assignment import SlotAssignmentPlan


def publish_slot_plan(
    client,
    *,
    ams_id: int,
    tray_id: int,
    plan: SlotAssignmentPlan,
    tray_sub_brands: str = "",
    tray_type_fallback: str = "",
) -> bool:
    sent = client.ams_set_filament_setting(
        ams_id=ams_id,
        tray_id=tray_id,
        tray_info_idx=plan.tray_info_idx,
        tray_type=plan.tray_type or tray_type_fallback,
        tray_sub_brands=tray_sub_brands,
        tray_color=plan.tray_color,
        nozzle_temp_min=plan.nozzle_temp_min,
        nozzle_temp_max=plan.nozzle_temp_max,
        setting_id=plan.setting_id,
        cols=plan.cols,
        ctype=plan.ctype,
    )
    if sent:
        # Read-back verification (upstream #2582): the printer echoes the
        # accepted filament id in its next per-tray push, so we record what we
        # just ADVERTISED — verifying against the actual plan would report every
        # projected slot as rejected. ``cali_idx`` starts unknown because the
        # caller's K-profile push resolves it live inside
        # ``apply_active_calibration_to_slot``, which fills it in afterwards.
        client.register_assignment_verification(
            ams_id=ams_id, tray_id=tray_id, tray_info_idx=plan.tray_info_idx, tray_color=plan.tray_color, cali_idx=None
        )
    return bool(sent)
