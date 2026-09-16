"""Bulk apply: preview publishes nothing; apply re-advertises only what the policy projects; startup rebuilds the overlay."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from backend.app.models.printer import Printer
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services import ams_advertised_overlay as overlay, ams_backup_compatibility_apply as bulk

MANUAL = {"tag_uid": "0000000000000000", "tray_uuid": "", "state": 11, "exists": True}


async def _farm(db_session):
    printer = Printer(
        name="P1S",
        serial_number="01P00A000000003",
        ip_address="192.168.1.52",
        access_code="12345678",
        model="P1S",
        ams_policies={"backup_compatibility": {"normalize_color": True}},
    )
    red = Spool(material="PETG", rgba="FF0000FF", filament_family_id="GFG99", label_weight=1000)
    rfid = Spool(
        material="PETG",
        rgba="0000FFFF",
        filament_family_id="GFG99",
        label_weight=1000,
        tray_uuid="0123456789ABCDEF0123456789ABCDEF",
    )
    db_session.add_all([printer, red, rfid])
    await db_session.commit()
    for s in (printer, red, rfid):
        await db_session.refresh(s)
    db_session.add_all(
        [
            SpoolAssignment(spool_id=red.id, printer_id=printer.id, ams_id=0, tray_id=0, fingerprint_type="PETG"),
            SpoolAssignment(spool_id=rfid.id, printer_id=printer.id, ams_id=0, tray_id=1, fingerprint_type="PETG"),
            SpoolAssignment(spool_id=red.id, printer_id=printer.id, ams_id=0, tray_id=2, fingerprint_type=""),
        ]
    )
    await db_session.commit()
    state = SimpleNamespace(
        nozzles=[],
        support_user_preset=False,
        raw_data={
            "ams": [
                {
                    "id": 0,
                    "tray": [
                        dict(MANUAL, id=0, tray_type="PETG", tray_color="FF0000FF", tray_info_idx="GFG99", cali_idx=3),
                        dict(
                            MANUAL,
                            id=1,
                            tray_type="PETG",
                            tray_color="0000FFFF",
                            tray_info_idx="GFG99",
                            tray_uuid="0123456789ABCDEF0123456789ABCDEF",
                        ),
                        {"id": 2},  # the slot is empty
                    ],
                }
            ]
        },
    )
    return printer, state


@pytest.mark.asyncio
async def test_dry_run_lists_apply_and_skip_rows_and_publishes_nothing(db_session):
    printer, state = await _farm(db_session)
    client = MagicMock()
    with patch("backend.app.services.ams_backup_compatibility_apply.printer_manager") as pm:
        pm.get_status.return_value = state
        result = await bulk.bulk_apply(db_session, printer, client, dry_run=True)
    by_slot = {(r["ams_id"], r["tray_id"]): r for r in result["rows"]}
    assert by_slot[(0, 0)]["action"] == "apply" and by_slot[(0, 0)]["advertised"]["tray_color"] == "000000FF"
    assert by_slot[(0, 1)]["action"] == "skip" and by_slot[(0, 1)]["reasons"] == ["rfid_slot_excluded"]
    assert by_slot[(0, 2)]["action"] == "skip" and "slot_empty" in by_slot[(0, 2)]["reasons"]
    assert result["dry_run"] is True and result["applied"] == 0 and result["would_apply"] == 1
    client.ams_set_filament_setting.assert_not_called()
    assert overlay.entries_for(printer.id) == {}


@pytest.mark.asyncio
async def test_apply_publishes_only_apply_rows_keeps_live_k_and_fills_the_overlay(db_session):
    printer, state = await _farm(db_session)
    client = MagicMock()
    client.ams_set_filament_setting.return_value = True
    with patch("backend.app.services.ams_backup_compatibility_apply.printer_manager") as pm:
        pm.get_status.return_value = state
        result = await bulk.bulk_apply(db_session, printer, client, dry_run=False)
    assert (result["applied"], result["skipped"]) == (1, 2)
    client.ams_set_filament_setting.assert_called_once()
    assert client.ams_set_filament_setting.call_args.kwargs["tray_color"] == "000000FF"
    client.extrusion_cali_sel.assert_called_once()
    assert client.extrusion_cali_sel.call_args.kwargs["cali_idx"] == 3
    assert (0, 0) in overlay.entries_for(printer.id) and (0, 1) not in overlay.entries_for(printer.id)


@pytest.mark.asyncio
async def test_policy_off_reverts_a_slot_the_overlay_still_remembers(db_session):
    printer, state = await _farm(db_session)
    printer.ams_policies = {}  # policy switched off after the slot was advertised black
    await db_session.commit()
    from backend.app.services.ams_advertised_overlay import OverlayEntry

    overlay.replace_printer(
        printer.id, {(0, 0): OverlayEntry("PETG", "FF0000FF", "GFG99", (), "000000FF", "GFG99", "internal")}
    )
    state.raw_data["ams"][0]["tray"][0]["tray_color"] = "000000FF"  # the printer still shows black
    client = MagicMock()
    client.ams_set_filament_setting.return_value = True
    with patch("backend.app.services.ams_backup_compatibility_apply.printer_manager") as pm:
        pm.get_status.return_value = state
        result = await bulk.bulk_apply(db_session, printer, client, dry_run=False)
    row = next(r for r in result["rows"] if (r["ams_id"], r["tray_id"]) == (0, 0))
    assert row["action"] == "revert" and row["published"] is True
    assert client.ams_set_filament_setting.call_args.kwargs["tray_color"] == "FF0000FF"
    assert (0, 0) not in overlay.entries_for(printer.id)


@pytest.mark.asyncio
async def test_refresh_overlay_rebuilds_from_the_registry(db_session):
    printer, state = await _farm(db_session)
    with patch("backend.app.services.ams_backup_compatibility_apply.printer_manager") as pm:
        pm.get_status.return_value = state
        await bulk.refresh_overlay(db_session, printer)
    entries = overlay.entries_for(printer.id)
    # (0,2) is projected too: emptiness is a bulk question, not a projection one.
    assert set(entries) == {(0, 0), (0, 2)} and entries[(0, 0)].actual_color == "FF0000FF"
