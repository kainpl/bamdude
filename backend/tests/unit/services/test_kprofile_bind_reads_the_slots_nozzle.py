"""Every K-profile bind asks about the nozzle its slot feeds (upstream e5a18bf5).

Six binders — the spool assign, the pre-print hook, the RFID auto-assign, the
RFID re-read, the drift re-apply in ``on_ams_change`` and the Spoolman routes —
each rebuilt "the nozzle" by hand: ``state.nozzles[0]`` for the diameter whatever
the slot, and ``getattr(state, "nozzle_volume_type", "standard")`` for the flow,
an attribute the printer state never had. So a High Flow calibration was never
found, a slot on an H2D's second hotend was matched against the first hotend's
diameter, and the RFID path and the external holder always said extruder 0.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services.bambu_mqtt import NozzleInfo


def _h2d_state(**extra):
    """AMS 0 feeds the left hotend (extruder 1: 0.6 High Flow); AMS 1 the right
    (extruder 0: 0.4 Standard)."""
    return SimpleNamespace(
        connected=True,
        nozzles=[
            NozzleInfo(nozzle_diameter="0.4", nozzle_flow="standard"),
            NozzleInfo(nozzle_diameter="0.6", nozzle_flow="high_flow"),
        ],
        ams_extruder_map={"0": 1, "1": 0},
        support_user_preset=False,
        **extra,
    )


def _bind_args(apply_mock) -> list[tuple]:
    return [
        (
            c.kwargs["ams_id"],
            c.kwargs["slot_id"],
            c.kwargs["nozzle_diameter"],
            c.kwargs["nozzle_volume_type"],
            c.kwargs["extruder_id"],
        )
        for c in apply_mock.await_args_list
    ]


@pytest.mark.asyncio
async def test_the_pre_print_hook_binds_each_slot_against_its_own_nozzle(db_session):
    from backend.app.services.background_dispatch import _apply_calibrations_for_print

    state = _h2d_state(
        raw_data={
            "ams": [
                {"id": 0, "tray": [{"id": 0, "tray_info_idx": "GFL99"}]},
                {"id": 1, "tray": [{"id": 0, "tray_info_idx": "GFL99"}]},
            ],
            "vt_tray": [{"id": 254, "tray_info_idx": "GFL99"}, {"id": 255, "tray_info_idx": "GFL99"}],
        }
    )
    client = MagicMock(state=SimpleNamespace(connected=True))
    apply = AsyncMock(return_value=(False, None))
    with (
        patch("backend.app.services.background_dispatch.printer_manager") as pm,
        patch("backend.app.services.calibration_service.apply_active_calibration_to_slot", new=apply),
    ):
        pm.get_client.return_value = client
        pm.get_status.return_value = state
        await _apply_calibrations_for_print(db_session, 1, ams_mapping=None)

    assert sorted(_bind_args(apply)) == [
        (0, 0, 0.6, "high_flow", 1),
        (1, 0, 0.4, "standard", 0),
        (255, 0, 0.6, "high_flow", 1),  # Ext-L is the left hotend, not extruder 0
        (255, 1, 0.4, "standard", 0),
    ]


@pytest.mark.asyncio
async def test_assigning_a_spool_binds_against_the_slots_nozzle(db_session):
    from backend.app.api.routes.inventory import apply_spool_to_slot_via_mqtt
    from backend.app.models.printer import Printer
    from backend.app.models.spool import Spool

    printer = Printer(
        name="H2D", serial_number="0948AD000000001", ip_address="192.168.1.52", access_code="x", model="H2D"
    )
    spool = Spool(material="PLA", brand="Bambu", rgba="FF0000FF", filament_family_id="GFA00", label_weight=1000)
    db_session.add_all([printer, spool])
    await db_session.commit()
    state = _h2d_state(raw_data={"ams": [{"id": 0, "tray": [{"id": 1, "tray_type": "PLA", "tray_info_idx": "GFA00"}]}]})
    client = MagicMock()
    client.ams_set_filament_setting.return_value = True
    apply = AsyncMock(return_value=(False, None))
    with (
        patch("backend.app.services.printer_manager.printer_manager") as pm,
        patch("backend.app.services.calibration_service.apply_active_calibration_to_slot", new=apply),
    ):
        pm.get_client.return_value = client
        pm.get_status.return_value = state
        pm.get_model.return_value = "H2D"
        await apply_spool_to_slot_via_mqtt(
            db=db_session, current_user=None, spool=spool, printer_id=printer.id, ams_id=0, tray_id=1
        )

    assert _bind_args(apply) == [(0, 1, 0.6, "high_flow", 1)]


def test_no_binder_reads_a_flow_the_printer_state_does_not_have():
    """``PrinterState`` has no ``nozzle_volume_type``; a ``getattr`` of it with a
    default is a constant, not a lookup. Scans the source so a seventh binder
    written the old way is a red test, not a silent "standard"."""
    root = Path(__file__).resolve().parents[3] / "app"
    offenders = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "nozzle_volume_type"
            ):
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert offenders == []
