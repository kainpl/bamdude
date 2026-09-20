"""Both Spoolman publish paths: default-off is today's payload byte for byte.

``test_apply_spool_projection`` pins the third path (internal assign + replay).
These two pin the other two, because the refactor onto ``publish_slot_plan``
moved the ``ams_set_filament_setting`` call out of each route — and a payload
that quietly changed shape would be a slot the printer configures differently
for every install that never turns the policy on, i.e. everyone today.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.api.routes._spoolman_helpers import _map_spoolman_spool
from backend.app.models.printer import Printer
from backend.app.models.settings import Settings
from backend.app.services.slot_assignment import build_slot_assignment

SPOOL_ID = 77
SPOOLMAN_RAW = {
    "id": SPOOL_ID,
    "used_weight": 0,
    "filament": {
        "material": "PETG",
        "name": "PETG HF",
        "color_hex": "00FF00",
        "weight": 1000,
        "vendor": {"name": "Bambu Lab"},
    },
}
# A manual (non-RFID) tray, so nothing but the policy could hold a projection back.
LIVE_TRAY = {
    "id": 1,
    "tray_type": "PETG",
    "tray_color": "00FF00FF",
    "tray_info_idx": "GFG99",
    "tag_uid": "0000000000000000",
    "tray_uuid": "",
}


async def _printer(db_session, serial: str) -> Printer:
    printer = Printer(
        name="P1S",
        serial_number=serial,
        ip_address="192.168.1.60",
        access_code="12345678",
        model="P1S",
        ams_policies={},  # policy off — today's behaviour
    )
    db_session.add_all(
        [
            printer,
            Settings(key="spoolman_enabled", value="true"),
            Settings(key="spoolman_url", value="http://localhost:7912"),
        ]
    )
    await db_session.commit()
    await db_session.refresh(printer)
    return printer


def _state() -> SimpleNamespace:
    return SimpleNamespace(
        nozzles=[],
        ams_extruder_map={},
        support_user_preset=False,
        nozzle_volume_type="standard",
        raw_data={"ams": [{"id": 0, "tray": [LIVE_TRAY]}]},
    )


def _spoolman_stub() -> SimpleNamespace:
    return SimpleNamespace(
        get_spool=AsyncMock(return_value=SPOOLMAN_RAW),
        merge_spool_extra=AsyncMock(return_value={"id": SPOOL_ID, "extra": {}}),
        health_check=AsyncMock(return_value=True),
    )


async def _expected(db_session, mqtt: MagicMock) -> dict:
    """Today's payload, rebuilt from the same builder the route feeds."""
    mapped = _map_spoolman_spool(SPOOLMAN_RAW)
    plan = await build_slot_assignment(
        db_session,
        family_id=None,  # no linked K-profile in this fixture → the generic family
        material_override=mapped["material"],
        color_rgba="00FF00FF",
        temp_overrides=(mapped.get("nozzle_temp_min"), None),
        printer_model="P1S",
        nozzle_diameter="0.4",
        supports_user_preset=False,
    )
    assert mqtt.register_assignment_verification.call_args.kwargs["tray_info_idx"] == plan.tray_info_idx
    return {
        "ams_id": 0,
        "tray_id": 1,
        "tray_info_idx": plan.tray_info_idx,
        "tray_type": plan.tray_type or mapped["material"],
        "tray_sub_brands": "Bambu Lab PETG HF",
        "tray_color": "00FF00FF",
        "nozzle_temp_min": plan.nozzle_temp_min,
        "nozzle_temp_max": plan.nozzle_temp_max,
        "setting_id": plan.setting_id,
        "cols": plan.cols,
        "ctype": plan.ctype,
    }


@pytest.mark.asyncio
async def test_spoolman_assign_publishes_todays_payload_byte_for_byte(db_session):
    from backend.app.api.routes import spoolman_inventory
    from backend.app.api.routes.spoolman_inventory import SpoolSlotAssignmentRequest

    printer = await _printer(db_session, "01P00A000000010")
    mqtt = MagicMock()
    mqtt.ams_set_filament_setting.return_value = True
    with (
        patch.object(spoolman_inventory, "_get_client", new=AsyncMock(return_value=_spoolman_stub())),
        patch.object(spoolman_inventory, "_clear_stale_slot_fallback_tag_links", new=AsyncMock()),
        patch.object(spoolman_inventory, "printer_manager") as pm,
        patch(
            "backend.app.services.calibration_service.apply_active_calibration_to_slot",
            new=AsyncMock(return_value=(False, None)),
        ),
    ):
        pm.get_client.return_value = mqtt
        pm.get_status.return_value = _state()
        pm.get_model.return_value = "P1S"
        await spoolman_inventory.assign_spoolman_slot(
            SpoolSlotAssignmentRequest(printer_id=printer.id, ams_id=0, tray_id=1, spoolman_spool_id=SPOOL_ID),
            db=db_session,
        )
    assert mqtt.ams_set_filament_setting.call_args.kwargs == await _expected(db_session, mqtt)


@pytest.mark.asyncio
async def test_spoolman_link_publishes_todays_payload_byte_for_byte(db_session):
    from backend.app.api.routes import spoolman as spoolman_routes

    printer = await _printer(db_session, "01P00A000000011")
    mqtt = MagicMock()
    mqtt.ams_set_filament_setting.return_value = True
    with (
        patch.object(spoolman_routes, "get_spoolman_client", new=AsyncMock(return_value=_spoolman_stub())),
        patch.object(spoolman_routes, "_clear_stale_tag_links", new=AsyncMock()),
        patch.object(spoolman_routes, "printer_manager") as pm,
        patch(
            "backend.app.services.calibration_service.apply_active_calibration_to_slot",
            new=AsyncMock(return_value=(False, None)),
        ),
    ):
        pm.get_client.return_value = mqtt
        pm.get_status.return_value = _state()
        pm.get_model.return_value = "P1S"
        await spoolman_routes.link_spool(
            SPOOL_ID,
            spoolman_routes.LinkSpoolRequest(
                tray_uuid="A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4",
                printer_id=printer.id,
                ams_id=0,
                tray_id=1,
            ),
            db=db_session,
        )
    assert mqtt.ams_set_filament_setting.call_args.kwargs == await _expected(db_session, mqtt)
