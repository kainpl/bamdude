"""One publisher for ams_filament_setting: the plan's fields, nothing else, verification registered on success."""

from unittest.mock import MagicMock

from backend.app.services.slot_assignment import SlotAssignmentPlan
from backend.app.services.slot_assignment_publish import publish_slot_plan


def _plan(**kw):
    base = {
        "tray_info_idx": "GFG99",
        "setting_id": "GFSG99_00",
        "tray_type": "PETG",
        "tray_color": "000000FF",
        "cols": [],
        "ctype": 0,
        "nozzle_temp_min": 230,
        "nozzle_temp_max": 260,
    }
    base.update(kw)
    return SlotAssignmentPlan(**base)


def test_publishes_every_plan_field_and_registers_verification():
    client = MagicMock()
    client.ams_set_filament_setting.return_value = True
    assert (
        publish_slot_plan(
            client,
            ams_id=0,
            tray_id=2,
            plan=_plan(cols=["000000FF", "112233FF"], ctype=1),
            tray_sub_brands="Brand PETG",
        )
        is True
    )
    client.ams_set_filament_setting.assert_called_once_with(
        ams_id=0,
        tray_id=2,
        tray_info_idx="GFG99",
        tray_type="PETG",
        tray_sub_brands="Brand PETG",
        tray_color="000000FF",
        nozzle_temp_min=230,
        nozzle_temp_max=260,
        setting_id="GFSG99_00",
        cols=["000000FF", "112233FF"],
        ctype=1,
    )
    client.register_assignment_verification.assert_called_once_with(
        ams_id=0, tray_id=2, tray_info_idx="GFG99", tray_color="000000FF", cali_idx=None
    )


def test_empty_tray_type_falls_back_and_no_verification_when_not_sent():
    client = MagicMock()
    client.ams_set_filament_setting.return_value = False
    assert publish_slot_plan(client, ams_id=1, tray_id=0, plan=_plan(tray_type=""), tray_type_fallback="PETG") is False
    assert client.ams_set_filament_setting.call_args.kwargs["tray_type"] == "PETG"
    client.register_assignment_verification.assert_not_called()
