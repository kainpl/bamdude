"""The ONE publisher of a SlotAssignmentPlan to an AMS slot.

Every inventory assignment path (internal assign + replay, Spoolman assign,
Spoolman link) publishes through here, so a projected plan
(services/ams_backup_compatibility) reaches the wire from exactly one place and
the read-back verification always expects what was actually sent. The manual
``configure_ams_slot`` route keeps its own order (verification after cali_idx)
and is deliberately not a caller.

Two layers: ``publish_slot_plan`` puts a plan on the wire, and
``publish_projected_slot`` is the whole "project, publish, remember" step the
three assignment routes each used to carry a copy of.
"""

from __future__ import annotations

import logging

from backend.app.services import ams_advertised_overlay as overlay
from backend.app.services.ams_backup_compatibility import (
    BackupCompatibilityPolicy,
    SlotProjection,
    project_slot_assignment,
)
from backend.app.services.slot_assignment import SlotAssignmentPlan

logger = logging.getLogger(__name__)


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


async def publish_projected_slot(
    db,
    client,
    *,
    printer,
    printer_id: int,
    ams_id: int,
    tray_id: int,
    actual_plan: SlotAssignmentPlan,
    live_tray: dict | None,
    spool_tag_uid: str | None,
    spool_tray_uuid: str | None,
    material: str | None,
    extra_colors: str | None,
    printer_model: str | None,
    nozzle_diameter: str,
    supports_user_preset: bool,
    tray_sub_brands: str = "",
    tray_type_fallback: str = "",
    source: str,
) -> tuple[bool, SlotProjection]:
    """Project one slot under the printer's policy, publish it, remember what was masked.

    The three assignment routes each carried a copy of these four steps, which
    is three places for the order to drift — and the order is the whole
    contract: the ACTUAL plan is the spool's truth, only the ADVERTISED one
    reaches the wire, and an entry is remembered ONLY for a payload that
    actually left the process (a disconnected printer was never told anything,
    so nothing is masked and routing must keep reading the live tray).

    Returns ``(sent, projection)``. The caller keeps the K-profile decision:
    ``kprofile_allowed(projection)`` is asked beside the calibration push,
    which keys off the ACTUAL family in every case.
    """
    projection = await project_slot_assignment(
        db,
        actual=actual_plan,
        policy=BackupCompatibilityPolicy.from_printer(printer),
        live_tray=live_tray,
        spool_tag_uid=spool_tag_uid,
        spool_tray_uuid=spool_tray_uuid,
        ams_id=ams_id,
        material=material,
        extra_colors=extra_colors,
        printer_model=printer_model,
        nozzle_diameter=nozzle_diameter,
        supports_user_preset=supports_user_preset,
    )
    if projection.projected:
        logger.info(
            "Slot assign (%s): advertising %s for AMS%d-T%d (%s)",
            source,
            projection.applied,
            ams_id,
            tray_id,
            projection.reasons,
        )
    sent = publish_slot_plan(
        client,
        ams_id=ams_id,
        tray_id=tray_id,
        plan=projection.advertised,
        tray_sub_brands=tray_sub_brands,
        tray_type_fallback=tray_type_fallback,
    )
    # The store is written HERE, before the caller commits its assignment row —
    # and that direction is the safe one. An entry without its row is inert: it
    # is keyed by slot and only speaks while the printer still echoes what we
    # published, and the very next rebuild (or a failed commit's re-assign)
    # replaces it. A row without its entry is the harmful order — routing would
    # read the mask as the spool for the whole life of the process.
    if sent:
        overlay.remember(printer_id, ams_id, tray_id, projection, source)
    return sent, projection
