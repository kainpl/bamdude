"""What a direct-connected rack (H3C, P3C) is sent, and what it is not yet (N01 stages A, B).

* Stage B: a print goes through the one dispatch layer, and the slot setting
  (``ams_filament_setting``, its reset) goes out in Bambu Studio's form — an
  assignment on a rack slot configures it like any slot.
* Still refused until stage C, by name: loading and unloading, an RFID re-read,
  drying, the K-profile of a slot and the automatic calibration. A manual
  calibration prints, once the hotend its tray leads to matches the session.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from backend.app.services import direct_rack_gate
from backend.app.services.bambu_mqtt import BambuMQTTClient
from backend.app.utils.tray_ids import AmsType, TrayLayout


@pytest.fixture
async def spool_factory(db_session):
    from backend.app.models.spool import Spool

    async def _create(**kwargs):
        spool = Spool(
            **{
                "material": "PLA",
                "subtype": "Basic",
                "brand": "Generic",
                "color_name": "Red",
                "rgba": "FF0000FF",
                "label_weight": 1000,
                "weight_used": 0,
                **kwargs,
            }
        )
        db_session.add(spool)
        await db_session.commit()
        await db_session.refresh(spool)
        return spool

    return _create


H3C = TrayLayout(units=((32, AmsType.ZMULTI_A),), direct_rack=True, rack_size=9)
P3C = TrayLayout(units=((33, AmsType.ZMULTI_B),), direct_rack=True, rack_size=7)
X1C = TrayLayout(units=((0, AmsType.AMS),))


def test_the_gate():
    assert direct_rack_gate.slot_command_refused(H3C, 32, 5)
    assert direct_rack_gate.slot_command_refused(H3C, 255, 4)
    assert direct_rack_gate.is_rack_slot(H3C, 32, 5)
    assert not direct_rack_gate.slot_command_refused(H3C, 0, 1)  # a bound regular AMS is not a rack slot
    assert not direct_rack_gate.slot_command_refused(X1C, 255, 0)
    assert direct_rack_gate.tray_command_refused(H3C, 34)
    assert direct_rack_gate.tray_command_refused(H3C, 254)
    assert not direct_rack_gate.tray_command_refused(X1C, 254)
    assert direct_rack_gate.auto_calibration_refused(P3C) and not direct_rack_gate.auto_calibration_refused(X1C)
    # A stand-in (a mocked manager's answer) is no layout, never "a rack".
    assert not direct_rack_gate.auto_calibration_refused(MagicMock())
    # Stage B: printing is no longer the gate's.
    assert not hasattr(direct_rack_gate, "print_refused")


def _client(model: str, units: list[dict]) -> BambuMQTTClient:
    client = BambuMQTTClient(ip_address="192.0.2.9", serial_number=f"S-{model}", access_code="x", model=model)
    client._process_message({"print": {"ams": {"ams": units}}})
    client._client = MagicMock()
    client.state.connected = True
    return client


@pytest.mark.parametrize(
    ("model", "units", "ams_id", "tray_id", "sent"),
    [
        ("H3C", [{"id": "32", "info": "240842026", "tray": []}], 32, 5, True),
        ("H3C", [{"id": "32", "info": "240842026", "tray": []}], 255, 4, True),
        ("X1C", [{"id": "0", "info": "1001", "tray": []}], 0, 1, True),
        ("H2D", [{"id": "0", "info": "1001", "tray": []}], 255, 0, True),
    ],
)
def test_the_slot_setting_writer(model, units, ams_id, tray_id, sent):
    client = _client(model, units)
    result = client.ams_set_filament_setting(
        ams_id, tray_id, "GFL99", "PLA", "", "FFFFFFFF", 190, 230, setting_id="GFSL99"
    )
    assert bool(result) is sent
    assert client._client.publish.called is sent


def test_load_and_drying_writers_refuse_on_a_rack():
    client = _client("H3C", [{"id": "32", "info": "240842026", "tray": []}])
    assert client.ams_load_filament(34) is False
    assert client.ams_load_filament(254) is False
    assert client.send_drying_command(32, 55, 4) is False
    assert client.ams_refresh_tray(32, 3) == (False, direct_rack_gate.UNSUPPORTED_MESSAGE)
    assert not client._client.publish.called


def _status(units):
    status = MagicMock()
    status.raw_data = {"ams": {"ams": units}}
    status.nozzles = [MagicMock(nozzle_diameter="0.4")]
    status.ams_extruder_map = None
    status.state = "IDLE"
    return status


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    ("layout", "ams_id", "tray_id", "published"),
    [(H3C, 32, 5, True), (X1C, 0, 1, True)],
    ids=["rack-slot", "x1c"],
)
async def test_assignment_on_a_rack_configures_the_slot(
    async_client: AsyncClient, printer_factory, spool_factory, layout, ams_id, tray_id, published
):
    printer = await printer_factory(name="Rack", model="H3C" if layout.direct_rack else "X1C")
    spool = await spool_factory(material="PLA")
    mqtt = MagicMock()
    mqtt.ams_set_filament_setting.return_value = True
    mqtt.extrusion_cali_sel.return_value = True
    unit = {"id": ams_id if ams_id != 255 else 0, "tray": [{"id": tray_id, "state": 11, "tray_type": "PLA"}]}
    with patch("backend.app.services.printer_manager.printer_manager") as pm:
        pm.ensure_fresh_connection = AsyncMock(return_value=True)
        pm.get_client.return_value = mqtt
        pm.get_status.return_value = _status([unit])
        pm.tray_layout.return_value = layout
        response = await async_client.post(
            "/api/v1/inventory/assignments",
            json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": ams_id, "tray_id": tray_id},
        )
    assert response.status_code == 200, response.text
    assert response.json()["pending_config"] is (not published)
    assert mqtt.ams_set_filament_setting.called is published
    if layout.direct_rack:
        # The K-profile of a rack slot waits for stage C (keyed by nozzle_pos in Studio).
        assert not mqtt.extrusion_cali_sel.called


@pytest.mark.asyncio
@pytest.mark.integration
async def test_assignment_beyond_a_p3c_rack_is_refused(async_client: AsyncClient, printer_factory, spool_factory):
    printer = await printer_factory(name="P3C", model="P3C")
    spool = await spool_factory(material="PLA")
    with patch("backend.app.services.printer_manager.printer_manager") as pm:
        pm.ensure_fresh_connection = AsyncMock(return_value=True)
        pm.get_client.return_value = MagicMock()
        pm.get_status.return_value = _status([])
        pm.tray_layout.return_value = P3C
        for ams_id, tray_id in ((33, 7), (255, 3)):
            response = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": ams_id, "tray_id": tray_id},
            )
            assert response.status_code == 422, (ams_id, tray_id, response.text)


# --- acceptance r1 (A1-01, A1-05) and the refusals the first round did not pin ---


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cali", [("auto_pa_line", "extrusion_cali_start"), ("flow_rate", "flow_rate_cali_start")], ids=["pa", "flow"]
)
async def test_filament_calibration_is_refused_on_a_rack_before_a_session(db_session, printer_factory, cali):
    from sqlalchemy import select

    from backend.app.models.calibration_session import CalibrationSession
    from backend.app.services.calibration_constants import CaliMethod, CaliMode
    from backend.app.services.calibration_mode_registry import ModeState
    from backend.app.services.calibration_service import CalibFilamentInput, CalibrationService

    mode, writer = cali
    printer = await printer_factory(model="H3C")
    client = MagicMock()
    client.state.connected = True
    with (
        patch("backend.app.services.calibration_service.printer_manager") as pm,
        patch("backend.app.services.calibration_service.get_mode_state", return_value=ModeState.PRODUCTION),
    ):
        pm.get_client.return_value = client
        pm.tray_layout.return_value = H3C
        with pytest.raises(direct_rack_gate.DirectRackUnsupported):
            await CalibrationService().start_calibration(
                db=db_session,
                printer_id=printer.id,
                cali_mode=CaliMode(mode),
                method=CaliMethod.AUTO,
                nozzle_diameter=0.4,
                nozzle_volume_type="standard",
                extruder_id=0,
                filaments=[
                    CalibFilamentInput(
                        ams_id=32,
                        slot_id=5,
                        tray_id=33,
                        filament_id="GFG00",
                        filament_setting_id="GFG00_60@BBL",
                        bed_temp=60,
                        nozzle_temp=220,
                        max_volumetric_speed=12.0,
                    )
                ],
                user_id=None,
            )
    assert not getattr(client, writer).called
    sessions = (await db_session.execute(select(CalibrationSession))).scalars().all()
    assert sessions == []  # nothing left to block the next start


def test_calibration_writers_refuse_on_a_rack_as_the_floor():
    client = _client("H3C", [{"id": "32", "info": "240842026", "tray": []}])
    filaments = [{"tray_id": 133, "ams_id": 32, "slot_id": 5, "filament_id": "GFG00"}]
    assert client.extrusion_cali_start(nozzle_diameter=0.4, cali_mode=0, filaments=filaments) == (False, None)
    assert client.flow_rate_cali_start(nozzle_diameter=0.4, filaments=filaments) == (False, None)
    assert not client._client.publish.called
    x1c = _client("X1C", [{"id": "0", "info": "1001", "tray": []}])
    ok, _ = x1c.extrusion_cali_start(nozzle_diameter=0.4, cali_mode=0, filaments=[{"tray_id": 0, "ams_id": 0}])
    assert ok and x1c._client.publish.called


@pytest.mark.asyncio
@pytest.mark.integration
async def test_autoqueue_no_longer_refuses_a_rack(db_session, printer_factory):
    from types import SimpleNamespace

    from backend.app.models.auto_queue import AutoQueueItem
    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.services.auto_queue_eligibility import find_eligible_printer
    from backend.app.services.filament_intake import routing_detail

    p = await printer_factory(name="Rack", serial_number="RACK01", model="H3C", is_active=True)
    db_session.add(PrinterQueue(printer_id=p.id, auto_distribute_eligible=True, is_paused=False))
    await db_session.commit()
    requirements = SimpleNamespace(status="ok", reason=None, model="H3C", resolved_plate_id=None)
    with patch(
        "backend.app.services.auto_queue_eligibility.read_item_requirements", new=AsyncMock(return_value=requirements)
    ):
        result = await find_eligible_printer(db_session, AutoQueueItem(target_model="H3C"), set())
    refused = [f for f in result.facts if f.printer_id == p.id]
    assert "direct_rack_unsupported" not in [f.code for f in refused]
    assert routing_detail("direct_rack_unsupported")["message"]  # the code stays known for rows that waited on it


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_offline_rack_keeps_its_slots_on_both_doors(async_client: AsyncClient, printer_factory, spool_factory):
    """A1-05: with no live client the built-in door reads the model, as the Spoolman door does."""
    printer = await printer_factory(name="Offline H3C", model="H3C")
    spool = await spool_factory(material="PLA")
    response = await async_client.post(
        "/api/v1/inventory/assignments",
        json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 32, "tray_id": 5},
    )
    # Stage B: as on any offline printer, nothing is sent and the row is kept.
    assert response.status_code == 200, response.text
    beyond = await async_client.post(
        "/api/v1/inventory/assignments",
        json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 32, "tray_id": 9},
    )
    assert beyond.status_code == 422


@pytest.mark.asyncio
async def test_a_manual_calibration_on_a_hotend_of_another_size_is_refused_before_a_session(
    db_session, printer_factory
):
    """B1-01: a calibration row is exempt from routing, so its start asks the hotend its tray leads to."""
    import copy

    from sqlalchemy import select

    from backend.app.models.calibration_session import CalibrationSession
    from backend.app.services.calibration_constants import CaliMethod, CaliMode
    from backend.app.services.calibration_mode_registry import ModeState
    from backend.app.services.calibration_service import CalibFilamentInput, CalibrationService
    from backend.app.services.printer_feed_snapshot import snapshot_from_state
    from backend.tests.unit.test_h3c_p3c_telemetry import H3C_FRAME

    h3c = BambuMQTTClient(ip_address="192.0.2.11", serial_number="H3CCALI", access_code="x", model="H3C")
    h3c._process_message({"print": copy.deepcopy(H3C_FRAME)})
    h3c.state.connected = True
    printer = await printer_factory(model="H3C")
    with (
        patch("backend.app.services.calibration_service.printer_manager") as pm,
        patch("backend.app.services.calibration_service.get_mode_state", return_value=ModeState.PRODUCTION),
    ):
        pm.get_client.return_value = h3c
        pm.tray_layout.return_value = h3c.tray_layout()
        pm.get_feed_snapshot.return_value = snapshot_from_state(printer.id, "H3C", h3c.state)
        with pytest.raises(direct_rack_gate.DirectRackRefused) as refused:
            await CalibrationService().start_calibration(
                db=db_session,
                printer_id=printer.id,
                cali_mode=CaliMode.PA_LINE,
                method=CaliMethod.MANUAL,
                nozzle_diameter=0.6,  # PrintTool 7 (AMS 3H slot 6) holds a 0.4
                nozzle_volume_type="high_flow",
                extruder_id=0,
                filaments=[
                    CalibFilamentInput(
                        ams_id=32,
                        slot_id=6,
                        tray_id=34,
                        filament_id="GFL99",
                        filament_setting_id="GFSL99",
                        bed_temp=60,
                        nozzle_temp=220,
                        max_volumetric_speed=12.0,
                    )
                ],
                user_id=None,
            )
    assert refused.value.code == "rack_hotend_mismatch"
    assert (await db_session.execute(select(CalibrationSession))).scalars().all() == []
