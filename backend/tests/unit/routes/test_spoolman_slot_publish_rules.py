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


async def run_route(
    db_session,
    route: str,
    printer: Printer,
    *,
    raw_spool: dict,
    tray: dict,
    kprofiles: tuple = (),
    real_k: bool = False,
    nozzles: tuple = (),
    extruder_map: dict | None = None,
) -> MagicMock:
    """Assign or link SPOOL_ID to AMS 0 slot 1; returns the MQTT client mock.

    ``real_k`` runs the real ``apply_active_calibration_to_slot`` against the
    same printer state instead of the "nothing fired" stand-in.
    """
    from backend.app.api.routes import spoolman as link_routes, spoolman_inventory as assign_routes

    stub = SimpleNamespace(
        get_spool=AsyncMock(return_value=raw_spool),
        merge_spool_extra=AsyncMock(return_value={"id": SPOOL_ID, "extra": {}}),
        link_native_tags=AsyncMock(return_value=[]),
        health_check=AsyncMock(return_value=True),
    )
    state = SimpleNamespace(
        nozzles=list(nozzles),
        ams_extruder_map=extruder_map or {},
        support_user_preset=False,
        nozzle_volume_type="standard",
        connected=True,
        kprofiles=list(kprofiles),
        raw_data={"ams": [{"id": 0, "tray": [tray]}]},
    )
    mqtt = MagicMock()
    mqtt.ams_set_filament_setting.return_value = True
    module = assign_routes if route == "assign" else link_routes
    if real_k:
        mqtt.state = state
        k_half = patch("backend.app.services.calibration_service.printer_manager")
    else:
        k_half = patch(
            "backend.app.services.calibration_service.apply_active_calibration_to_slot",
            new=AsyncMock(return_value=(False, None)),
        )
    with (
        patch.object(assign_routes, "_get_client", new=AsyncMock(return_value=stub)),
        patch.object(assign_routes, "_clear_stale_slot_fallback_tag_links", new=AsyncMock()),
        patch.object(link_routes, "get_spoolman_client", new=AsyncMock(return_value=stub)),
        patch.object(link_routes, "_clear_stale_tag_links", new=AsyncMock()),
        patch.object(module, "printer_manager") as pm,
        k_half as k_pm,
    ):
        if real_k:
            k_pm.get_client.return_value = mqtt
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


async def link_calibration(db_session, printer: Printer, filament_id: str) -> None:
    from backend.app.models.filament_calibration import FilamentCalibration
    from backend.app.models.spoolman_k_profile import SpoolmanKProfile

    fc = FilamentCalibration(
        printer_id=printer.id,
        filament_id=filament_id,
        nozzle_diameter=0.4,
        nozzle_volume_type="standard",
        extruder_id=0,
        cali_mode="pa",
        source="printer_sync",
        name=filament_id,
    )
    db_session.add(fc)
    await db_session.commit()
    await db_session.refresh(fc)
    db_session.add(
        SpoolmanKProfile(spoolman_spool_id=SPOOL_ID, printer_id=printer.id, extruder=0, filament_calibration_id=fc.id)
    )
    await db_session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
class TestTheSlotsFilamentFamily:
    """Linked calibration's family → the spool's stored preset → a stand-in.

    A Spoolman spool's preset (``extra.bambu_slicer_filament`` — set in the
    spool form, or taken from the tray when the sync created the spool) was read
    by the form alone: without a calibration linked for that printer and nozzle
    the slot was configured as the GENERIC family of the material, so a Bambu
    PETG HF spool went out as Generic PETG and AMS Backup grouped it by the
    wrong profile (upstream 5e3a2f278 / 53ef17dc5, issue #3433).
    """

    async def test_the_stored_preset_is_the_slots_family_when_no_calibration_is_linked(self, db_session, route):
        printer = await make_printer(db_session, f"01P00A0000005{ROUTES.index(route)}")

        mqtt = await run_route(
            db_session,
            route,
            printer,
            raw_spool=spoolman_spool(bambu_slicer_filament='"GFG02"'),
            tray=live_tray(),
        )

        sent = mqtt.ams_set_filament_setting.call_args.kwargs
        assert sent["tray_info_idx"] == "GFG02"
        assert sent["tray_type"] == "PETG"
        assert mqtt.register_assignment_verification.call_args.kwargs["tray_info_idx"] == "GFG02"

    async def test_a_linked_calibrations_family_still_wins(self, db_session, route):
        printer = await make_printer(db_session, f"01P00A0000006{ROUTES.index(route)}")
        await link_calibration(db_session, printer, "GFG00")  # Bambu PETG Basic

        mqtt = await run_route(
            db_session,
            route,
            printer,
            raw_spool=spoolman_spool(bambu_slicer_filament='"GFG02"'),
            tray=live_tray(),
        )

        assert mqtt.ams_set_filament_setting.call_args.kwargs["tray_info_idx"] == "GFG00"

    async def test_a_preset_nothing_resolves_falls_back_to_the_stand_in_and_keeps_the_spools_type(
        self, db_session, route
    ):
        printer = await make_printer(db_session, f"01P00A0000007{ROUTES.index(route)}")

        mqtt = await run_route(
            db_session,
            route,
            printer,
            raw_spool=spoolman_spool(bambu_slicer_filament='"no-such-preset"'),
            tray=live_tray(),
        )

        sent = mqtt.ams_set_filament_setting.call_args.kwargs
        assert sent["tray_info_idx"] == "GFG99"
        assert sent["tray_type"] == "PETG"

    async def test_a_stored_preset_of_another_material_does_not_configure_the_slot(self, db_session, route):
        """The spool's filament was changed in Spoolman (PLA → PETG) and the
        preset extra stayed behind: the slot is told the spool's material, not
        PLA Basic at PLA temperatures (review slots I-2)."""
        printer = await make_printer(db_session, f"01P00A0000009{ROUTES.index(route)}")

        mqtt = await run_route(
            db_session,
            route,
            printer,
            raw_spool=spoolman_spool(bambu_slicer_filament='"GFA00"'),  # Bambu PLA Basic on a PETG spool
            tray=live_tray(),
        )

        sent = mqtt.ams_set_filament_setting.call_args.kwargs
        assert sent["tray_info_idx"] == "GFG99"
        assert sent["tray_type"] == "PETG"
        assert sent["nozzle_temp_max"] > 240

    async def test_no_preset_and_no_calibration_is_the_stand_in_as_before(self, db_session, route):
        printer = await make_printer(db_session, f"01P00A0000008{ROUTES.index(route)}")

        mqtt = await run_route(db_session, route, printer, raw_spool=spoolman_spool(), tray=live_tray())

        assert mqtt.ams_set_filament_setting.call_args.kwargs["tray_info_idx"] == "GFG99"


def live_profile(cali_idx: int, filament_id: str, *, nozzle: str = "0.4", extruder: int = 0) -> SimpleNamespace:
    """One row of the printer's own K table (``state.kprofiles``)."""
    return SimpleNamespace(
        slot_id=cali_idx,
        extruder_id=extruder,
        nozzle_id="HS00-0.4",
        nozzle_diameter=nozzle,
        filament_id=filament_id,
        name=f"{filament_id} K",
        k_value="0.020000",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
class TestKeepingTheSlotsLiveK:
    """Re-sending the slot's filament may reset its K, so the slot's own live
    ``cali_idx`` is selected again. A profile belongs to ONE filament: when the
    spool's stored preset names the slot's family, the index goes back only if
    the live profile under it is that filament's — otherwise nothing is sent, as
    the built-in inventory sends nothing for a spool without a calibration."""

    async def test_no_stored_preset_reselects_the_live_index_as_before(self, db_session, route):
        printer = await make_printer(db_session, f"01P00A0000011{ROUTES.index(route)}")

        mqtt = await run_route(
            db_session,
            route,
            printer,
            raw_spool=spoolman_spool(),
            tray=live_tray(cali_idx=3),
            kprofiles=(live_profile(3, "GFA00"),),  # whatever the table says: not consulted
        )

        mqtt.extrusion_cali_sel.assert_called_once_with(
            ams_id=0, tray_id=1, cali_idx=3, filament_id="GFG99", nozzle_diameter="0.4"
        )

    async def test_a_stored_preset_keeps_a_live_profile_of_its_own_filament(self, db_session, route):
        printer = await make_printer(db_session, f"01P00A0000012{ROUTES.index(route)}")

        mqtt = await run_route(
            db_session,
            route,
            printer,
            raw_spool=spoolman_spool(bambu_slicer_filament='"GFG02"'),
            tray=live_tray(cali_idx=3),
            kprofiles=(live_profile(3, "GFG02"), live_profile(3, "GFG99", nozzle="0.6")),
        )

        mqtt.extrusion_cali_sel.assert_called_once_with(
            ams_id=0, tray_id=1, cali_idx=3, filament_id="GFG02", nozzle_diameter="0.4"
        )

    async def test_a_stored_preset_does_not_carry_another_filaments_k(self, db_session, route):
        printer = await make_printer(db_session, f"01P00A0000013{ROUTES.index(route)}")

        mqtt = await run_route(
            db_session,
            route,
            printer,
            raw_spool=spoolman_spool(bambu_slicer_filament='"GFG02"'),
            tray=live_tray(cali_idx=3),
            kprofiles=(live_profile(3, "GFG99"),),  # the K was chosen under Generic PETG
        )

        assert mqtt.ams_set_filament_setting.call_args.kwargs["tray_info_idx"] == "GFG02"
        mqtt.extrusion_cali_sel.assert_not_called()

    async def test_a_stored_preset_and_a_table_that_does_not_know_the_index_sends_nothing(self, db_session, route):
        printer = await make_printer(db_session, f"01P00A0000014{ROUTES.index(route)}")

        mqtt = await run_route(
            db_session,
            route,
            printer,
            raw_spool=spoolman_spool(bambu_slicer_filament='"GFG02"'),
            tray=live_tray(cali_idx=3),
            kprofiles=(live_profile(3, "GFG02", nozzle="0.6"),),  # another nozzle's table
        )

        mqtt.extrusion_cali_sel.assert_not_called()

    async def test_a_linked_calibrations_fallback_is_untouched(self, db_session, route):
        """The link's own bind is patched to "not fired" here; what follows it is
        the old re-select under the LINKED family, whatever the preset says."""
        printer = await make_printer(db_session, f"01P00A0000015{ROUTES.index(route)}")
        await link_calibration(db_session, printer, "GFG00")

        mqtt = await run_route(
            db_session,
            route,
            printer,
            raw_spool=spoolman_spool(bambu_slicer_filament='"GFG02"'),
            tray=live_tray(cali_idx=3),
            kprofiles=(live_profile(3, "GFG99"),),
        )

        mqtt.extrusion_cali_sel.assert_called_once_with(
            ams_id=0, tray_id=1, cali_idx=3, filament_id="GFG00", nozzle_diameter="0.4"
        )


async def active_calibration(
    db_session,
    printer: Printer,
    filament_id: str,
    *,
    nozzle: float = 0.4,
    flow: str = "standard",
    extruder: int = 0,
    linked: bool = False,
) -> None:
    """An ACTIVE calibration of the combo, named and valued as ``live_profile`` files it."""
    from backend.app.models.filament_calibration import FilamentCalibration
    from backend.app.models.spoolman_k_profile import SpoolmanKProfile

    fc = FilamentCalibration(
        printer_id=printer.id,
        filament_id=filament_id,
        nozzle_diameter=nozzle,
        nozzle_volume_type=flow,
        extruder_id=extruder,
        cali_mode="pa",
        source="printer_sync",
        name=f"{filament_id} K",
        pa_k_value=0.02,
        is_active=True,
    )
    db_session.add(fc)
    await db_session.commit()
    if linked:
        await db_session.refresh(fc)
        db_session.add(
            SpoolmanKProfile(
                spoolman_spool_id=SPOOL_ID, printer_id=printer.id, extruder=extruder, filament_calibration_id=fc.id
            )
        )
        await db_session.commit()


def fitted(diameter: str, flow: str = "") -> SimpleNamespace:
    return SimpleNamespace(nozzle_diameter=diameter, nozzle_flow=flow)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
class TestTheDefaultKOfTheSlotsFilament:
    """A spool gets its K by default through its filament: with no link made by
    hand, the ACTIVE calibration of (printer, the slot's filament, the slot's
    nozzle diameter, flow type, extruder) is selected — what the built-in
    inventory does for a new spool of a filament the printer already knows."""

    async def test_no_link_selects_the_active_calibration_of_the_slots_combo(self, db_session, route):
        printer = await make_printer(db_session, f"01P00A0000021{ROUTES.index(route)}")
        await active_calibration(db_session, printer, "GFG99")

        mqtt = await run_route(
            db_session,
            route,
            printer,
            raw_spool=spoolman_spool(),
            tray=live_tray(cali_idx=3),
            kprofiles=(live_profile(7, "GFG99"), live_profile(3, "GFA00")),
            real_k=True,
        )

        mqtt.extrusion_cali_sel.assert_called_once_with(
            ams_id=0, tray_id=1, cali_idx=7, filament_id="GFG99", nozzle_diameter="0.4"
        )

    async def test_the_combo_is_the_nozzle_the_slot_feeds(self, db_session, route):
        """AMS 0 feeds the LEFT hotend (extruder 1): a 0.6 High Flow nozzle."""
        printer = await make_printer(db_session, f"01P00A0000022{ROUTES.index(route)}")
        await active_calibration(db_session, printer, "GFG99")  # the right hotend's
        await active_calibration(db_session, printer, "GFG99", nozzle=0.6, flow="high_flow", extruder=1)

        mqtt = await run_route(
            db_session,
            route,
            printer,
            raw_spool=spoolman_spool(),
            tray=live_tray(),
            kprofiles=(live_profile(7, "GFG99"), live_profile(9, "GFG99", nozzle="0.6", extruder=1)),
            real_k=True,
            nozzles=(fitted("0.4", "standard"), fitted("0.6", "high_flow")),
            extruder_map={"0": 1},
        )

        mqtt.extrusion_cali_sel.assert_called_once_with(
            ams_id=0, tray_id=1, cali_idx=9, filament_id="GFG99", nozzle_diameter="0.6"
        )

    @pytest.mark.parametrize(
        "other",
        [
            {"nozzle": 0.6},
            {"extruder": 1},
            {"flow": "high_flow"},
            {"filament_id": "GFG00"},
        ],
        ids=["another-diameter", "another-extruder", "another-flow", "another-filament"],
    )
    async def test_a_calibration_of_another_combo_is_not_selected(self, db_session, route, other):
        printer = await make_printer(db_session, f"01P00A0000023{ROUTES.index(route)}")
        other = dict(other)
        filament_id = other.pop("filament_id", "GFG99")
        await active_calibration(db_session, printer, filament_id, **other)

        mqtt = await run_route(
            db_session,
            route,
            printer,
            raw_spool=spoolman_spool(),
            tray=live_tray(),  # no live index to keep either
            kprofiles=(
                live_profile(7, filament_id, nozzle=str(other.get("nozzle", "0.4")), extruder=other.get("extruder", 0)),
            ),
            real_k=True,
            nozzles=(fitted("0.4", "standard"), fitted("0.4", "standard")),
            extruder_map={"0": 0},
        )

        mqtt.extrusion_cali_sel.assert_not_called()

    async def test_a_link_made_by_hand_wins_over_the_default(self, db_session, route):
        printer = await make_printer(db_session, f"01P00A0000024{ROUTES.index(route)}")
        await active_calibration(db_session, printer, "GFG99")  # the stand-in's default
        await active_calibration(db_session, printer, "GFG00", linked=True)

        mqtt = await run_route(
            db_session,
            route,
            printer,
            raw_spool=spoolman_spool(),
            tray=live_tray(cali_idx=3),
            kprofiles=(live_profile(7, "GFG99"), live_profile(9, "GFG00")),
            real_k=True,
        )

        mqtt.extrusion_cali_sel.assert_called_once_with(
            ams_id=0, tray_id=1, cali_idx=9, filament_id="GFG00", nozzle_diameter="0.4"
        )

    async def test_a_stored_presets_own_calibration_is_the_default(self, db_session, route):
        """The live index was chosen under Generic PETG — not re-selected for
        the preset's family — but the preset's own calibration is."""
        printer = await make_printer(db_session, f"01P00A0000025{ROUTES.index(route)}")
        await active_calibration(db_session, printer, "GFG02")

        mqtt = await run_route(
            db_session,
            route,
            printer,
            raw_spool=spoolman_spool(bambu_slicer_filament='"GFG02"'),
            tray=live_tray(cali_idx=3),
            kprofiles=(live_profile(3, "GFG99"), live_profile(7, "GFG02")),
            real_k=True,
        )

        mqtt.extrusion_cali_sel.assert_called_once_with(
            ams_id=0, tray_id=1, cali_idx=7, filament_id="GFG02", nozzle_diameter="0.4"
        )

    async def test_a_calibration_the_live_table_lacks_keeps_the_live_index(self, db_session, route):
        printer = await make_printer(db_session, f"01P00A0000026{ROUTES.index(route)}")
        await active_calibration(db_session, printer, "GFG99")

        mqtt = await run_route(
            db_session,
            route,
            printer,
            raw_spool=spoolman_spool(),
            tray=live_tray(cali_idx=3),
            kprofiles=(live_profile(3, "GFA00"),),
            real_k=True,
        )

        mqtt.extrusion_cali_sel.assert_called_once_with(
            ams_id=0, tray_id=1, cali_idx=3, filament_id="GFG99", nozzle_diameter="0.4"
        )

    async def test_no_active_calibration_keeps_the_live_index_as_before(self, db_session, route):
        printer = await make_printer(db_session, f"01P00A0000027{ROUTES.index(route)}")

        mqtt = await run_route(
            db_session,
            route,
            printer,
            raw_spool=spoolman_spool(),
            tray=live_tray(cali_idx=3),
            kprofiles=(live_profile(3, "GFA00"),),
            real_k=True,
        )

        mqtt.extrusion_cali_sel.assert_called_once_with(
            ams_id=0, tray_id=1, cali_idx=3, filament_id="GFG99", nozzle_diameter="0.4"
        )

    async def test_a_slot_advertised_as_the_generic_under_the_skip_knob_gets_no_default(
        self, db_session, route, monkeypatch
    ):
        from backend.app.services import ams_backup_compatibility as compat

        printer = await make_printer(db_session, f"01P00A0000028{ROUTES.index(route)}")
        printer.ams_policies = {"backup_compatibility": {"generic_base_material": True}}
        await db_session.commit()
        await active_calibration(db_session, printer, "GFG02")
        monkeypatch.setattr(compat, "GENERIC_MODE_KPROFILE", "skip")

        mqtt = await run_route(
            db_session,
            route,
            printer,
            raw_spool=spoolman_spool(bambu_slicer_filament='"GFG02"'),
            tray=live_tray(),
            kprofiles=(live_profile(7, "GFG02"),),
            real_k=True,
        )

        assert mqtt.ams_set_filament_setting.call_args.kwargs["tray_info_idx"] == "GFG99"
        mqtt.extrusion_cali_sel.assert_not_called()
