"""What the two Spoolman publish paths (assign, tag link) send besides the payload.

* No read-back verification is registered for a slot the firmware calls empty
  — nothing will echo the filament back, so 30 s later the operator got a
  "not confirmed" warning for a pre-assignment that is fine — and a publish that
  went out asks the printer for a fresh report, so the check has current
  telemetry instead of waiting for an idle printer's next push (upstream
  53ef17dc5; the built-in assign route already did both).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.models.printer import Printer
from backend.app.models.settings import Settings

SPOOL_ID = 77
TAG = "A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4"


def spoolman_spool(**extra) -> dict:
    return {
        "id": SPOOL_ID,
        "used_weight": 0,
        "filament": {
            "material": "PETG",
            "name": "PETG HF",
            "color_hex": "00FF00",
            "weight": 1000,
            "vendor": {"name": "Bambu Lab"},
        },
        "extra": extra,
    }


def live_tray(**over) -> dict:
    # A manual (non-RFID) tray.
    return {
        "id": 1,
        "tray_type": "PETG",
        "tray_color": "00FF00FF",
        "tray_info_idx": "GFG99",
        "tag_uid": "0000000000000000",
        "tray_uuid": "",
        **over,
    }


async def make_printer(db_session, serial: str) -> Printer:
    printer = Printer(
        name="P1S",
        serial_number=serial,
        ip_address="192.168.1.60",
        access_code="12345678",
        model="P1S",
        ams_policies={},
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


async def run_route(db_session, route: str, printer: Printer, *, raw_spool: dict, tray: dict) -> MagicMock:
    """Assign or link SPOOL_ID to AMS 0 slot 1; returns the MQTT client mock."""
    from backend.app.api.routes import spoolman as link_routes, spoolman_inventory as assign_routes

    stub = SimpleNamespace(
        get_spool=AsyncMock(return_value=raw_spool),
        merge_spool_extra=AsyncMock(return_value={"id": SPOOL_ID, "extra": {}}),
        health_check=AsyncMock(return_value=True),
    )
    state = SimpleNamespace(
        nozzles=[],
        ams_extruder_map={},
        support_user_preset=False,
        nozzle_volume_type="standard",
        raw_data={"ams": [{"id": 0, "tray": [tray]}]},
    )
    mqtt = MagicMock()
    mqtt.ams_set_filament_setting.return_value = True
    module = assign_routes if route == "assign" else link_routes
    with (
        patch.object(assign_routes, "_get_client", new=AsyncMock(return_value=stub)),
        patch.object(assign_routes, "_clear_stale_slot_fallback_tag_links", new=AsyncMock()),
        patch.object(link_routes, "get_spoolman_client", new=AsyncMock(return_value=stub)),
        patch.object(link_routes, "_clear_stale_tag_links", new=AsyncMock()),
        patch.object(module, "printer_manager") as pm,
        patch(
            "backend.app.services.calibration_service.apply_active_calibration_to_slot",
            new=AsyncMock(return_value=(False, None)),
        ),
    ):
        pm.get_client.return_value = mqtt
        pm.get_status.return_value = state
        pm.get_model.return_value = "P1S"
        if route == "assign":
            await assign_routes.assign_spoolman_slot(
                assign_routes.SpoolSlotAssignmentRequest(
                    printer_id=printer.id, ams_id=0, tray_id=1, spoolman_spool_id=SPOOL_ID
                ),
                db=db_session,
            )
        else:
            await link_routes.link_spool(
                SPOOL_ID,
                link_routes.LinkSpoolRequest(tray_uuid=TAG, printer_id=printer.id, ams_id=0, tray_id=1),
                db=db_session,
            )
    return mqtt


ROUTES = ["assign", "link"]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
class TestReadBackVerification:
    async def test_a_loaded_slot_is_verified_and_a_fresh_report_is_requested(self, db_session, route):
        printer = await make_printer(db_session, f"01P00A0000001{ROUTES.index(route)}")

        mqtt = await run_route(db_session, route, printer, raw_spool=spoolman_spool(), tray=live_tray())

        mqtt.ams_set_filament_setting.assert_called_once()
        mqtt.register_assignment_verification.assert_called_once()
        mqtt.request_status_update.assert_called_once()

    @pytest.mark.parametrize("state", [9, 10])
    async def test_a_slot_the_firmware_calls_empty_is_published_but_not_verified(self, db_session, route, state):
        printer = await make_printer(db_session, f"01P00A0000002{ROUTES.index(route)}")

        mqtt = await run_route(db_session, route, printer, raw_spool=spoolman_spool(), tray=live_tray(state=state))

        # Still published: the Spoolman path has no replay-on-insert to configure it later.
        mqtt.ams_set_filament_setting.assert_called_once()
        mqtt.register_assignment_verification.assert_not_called()

    async def test_the_presence_bit_overrules_a_stale_empty_state(self, db_session, route):
        """An AMS-HT reports its LOADED tray as 9; the bit says a spool is there."""
        printer = await make_printer(db_session, f"01P00A0000003{ROUTES.index(route)}")

        mqtt = await run_route(
            db_session, route, printer, raw_spool=spoolman_spool(), tray=live_tray(state=9, exists=True)
        )

        mqtt.register_assignment_verification.assert_called_once()

    async def test_a_publish_that_did_not_go_out_requests_nothing(self, db_session, route):
        printer = await make_printer(db_session, f"01P00A0000004{ROUTES.index(route)}")
        from backend.app.services import slot_assignment_publish

        with patch.object(slot_assignment_publish, "publish_slot_plan", return_value=False):
            mqtt = await run_route(db_session, route, printer, raw_spool=spoolman_spool(), tray=live_tray())

        mqtt.request_status_update.assert_not_called()
