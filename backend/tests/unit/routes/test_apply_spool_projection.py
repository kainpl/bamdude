"""apply_spool_to_slot_via_mqtt: default-off is today's payload byte for byte; the policy changes only what is advertised."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.api.routes.inventory import apply_spool_to_slot_via_mqtt
from backend.app.models.printer import Printer
from backend.app.models.spool import Spool
from backend.app.services.slot_assignment import build_slot_assignment

LIVE_MANUAL_TRAY = {
    "id": 1,
    "tray_type": "PETG",
    "tray_color": "FF0000FF",
    "tray_info_idx": "GFG99",
    "tag_uid": "0000000000000000",
    "tray_uuid": "",
}


async def _fixture(db_session, policy: dict | None, tray=LIVE_MANUAL_TRAY):
    printer = Printer(
        name="P1S",
        serial_number="01P00A000000002",
        ip_address="192.168.1.51",
        access_code="12345678",
        model="P1S",
        ams_policies={"backup_compatibility": policy} if policy else {},
    )
    spool = Spool(
        material="PETG", brand="Bambu", subtype="", rgba="FF0000FF", filament_family_id="GFG99", label_weight=1000
    )
    db_session.add_all([printer, spool])
    await db_session.commit()
    await db_session.refresh(printer)
    await db_session.refresh(spool)
    state = SimpleNamespace(
        nozzles=[],
        ams_extruder_map={},
        support_user_preset=False,
        nozzle_volume_type="standard",
        raw_data={"ams": [{"id": 0, "tray": [tray]}]},
    )
    client = MagicMock()
    client.ams_set_filament_setting.return_value = True
    return printer, spool, state, client


async def _run(db_session, printer, spool, state, client):
    with (
        patch("backend.app.services.printer_manager.printer_manager") as pm,
        patch(
            "backend.app.services.calibration_service.apply_active_calibration_to_slot",
            new=AsyncMock(return_value=(False, None)),
        ) as cali,
    ):
        pm.get_client.return_value = client
        pm.get_status.return_value = state
        pm.get_model.return_value = "P1S"
        assert (
            await apply_spool_to_slot_via_mqtt(
                db=db_session, current_user=None, spool=spool, printer_id=printer.id, ams_id=0, tray_id=1
            )
            is True
        )
    return client.ams_set_filament_setting.call_args.kwargs, cali


@pytest.mark.asyncio
async def test_policy_off_publishes_todays_payload_byte_for_byte(db_session):
    printer, spool, state, client = await _fixture(db_session, None)
    sent, _ = await _run(db_session, printer, spool, state, client)
    plan = await build_slot_assignment(
        db_session, spool=spool, printer_model="P1S", nozzle_diameter="0.4", supports_user_preset=False
    )
    assert sent == {
        "ams_id": 0,
        "tray_id": 1,
        "tray_info_idx": plan.tray_info_idx,
        "tray_type": plan.tray_type,
        "tray_sub_brands": "Bambu PETG",
        "tray_color": "FF0000FF",
        "nozzle_temp_min": plan.nozzle_temp_min,
        "nozzle_temp_max": plan.nozzle_temp_max,
        "setting_id": plan.setting_id,
        "cols": [],
        "ctype": 0,
    }
    client.register_assignment_verification.assert_called_once()
    assert client.register_assignment_verification.call_args.kwargs["tray_info_idx"] == plan.tray_info_idx


@pytest.mark.asyncio
async def test_color_policy_advertises_black_but_verifies_the_same_family(db_session):
    printer, spool, state, client = await _fixture(
        db_session, {"normalize_color": True, "canonical_color_rgba": "000000FF"}
    )
    sent, cali = await _run(db_session, printer, spool, state, client)
    assert sent["tray_color"] == "000000FF" and sent["tray_info_idx"] == "GFG99"
    assert client.register_assignment_verification.call_args.kwargs["tray_color"] == "000000FF"
    assert spool.rgba == "FF0000FF"  # inventory untouched
    cali.assert_awaited_once()  # K-profile still follows the actual spool (GENERIC_MODE_KPROFILE == "actual")


@pytest.mark.asyncio
async def test_rfid_tray_gets_the_actual_payload_even_with_policy_on(db_session):
    rfid_tray = dict(LIVE_MANUAL_TRAY, tag_uid="A1B2C3D4E5F60718", tray_uuid="0123456789ABCDEF0123456789ABCDEF")
    printer, spool, state, client = await _fixture(
        db_session, {"normalize_color": True, "generic_base_material": True}, tray=rfid_tray
    )
    sent, _ = await _run(db_session, printer, spool, state, client)
    assert sent["tray_color"] == "FF0000FF"
