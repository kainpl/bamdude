"""N01 stage A: a direct-connected rack (H3C, P3C) is read, never written to (spec r2 §3.1).

* A1-01 — a command on a rack AMS slot or a rack's external input is not
  sent: the MQTT writer refuses, and an assignment there stays
  ``pending_config``. The same assignment on an X1C / H2D publishes as before.
* A1-02 — a print to a direct rack is refused at the one dispatch layer, by name.
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
    assert not direct_rack_gate.slot_command_refused(H3C, 0, 1)  # a bound regular AMS is not a rack slot
    assert not direct_rack_gate.slot_command_refused(X1C, 255, 0)
    assert direct_rack_gate.tray_command_refused(H3C, 34)
    assert direct_rack_gate.tray_command_refused(H3C, 254)
    assert not direct_rack_gate.tray_command_refused(X1C, 254)
    assert direct_rack_gate.print_refused(P3C) and not direct_rack_gate.print_refused(X1C)
    # A stand-in (a mocked manager's answer) is no layout, never "a rack".
    assert not direct_rack_gate.print_refused(MagicMock())


def _client(model: str, units: list[dict]) -> BambuMQTTClient:
    client = BambuMQTTClient(ip_address="192.0.2.9", serial_number=f"S-{model}", access_code="x", model=model)
    client._process_message({"print": {"ams": {"ams": units}}})
    client._client = MagicMock()
    client.state.connected = True
    return client


@pytest.mark.parametrize(
    ("model", "units", "ams_id", "tray_id", "sent"),
    [
        ("H3C", [{"id": "32", "info": "240842026", "tray": []}], 32, 5, False),
        ("H3C", [{"id": "32", "info": "240842026", "tray": []}], 255, 4, False),
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
    [(H3C, 32, 5, False), (H3C, 255, 4, False), (X1C, 0, 1, True)],
    ids=["rack-slot", "rack-external", "x1c"],
)
async def test_assignment_on_a_rack_stays_pending(
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


def test_direct_print_refused_by_name():
    from backend.app.services.background_dispatch import BackgroundDispatchService, DispatchEnqueueRejected

    service = BackgroundDispatchService()
    with patch("backend.app.services.background_dispatch.printer_manager") as pm:
        pm.tray_layout.return_value = H3C
        with pytest.raises(DispatchEnqueueRejected) as refused:
            service._refuse_unless_free(7, "H3C")
    assert refused.value.code == direct_rack_gate.UNSUPPORTED_CODE
    assert refused.value.detail()["message"] == direct_rack_gate.UNSUPPORTED_MESSAGE


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
async def test_the_dispatch_floor_refuses_whatever_reached_it():
    from backend.app.services.background_dispatch import BackgroundDispatchService

    service = BackgroundDispatchService()
    job = MagicMock(printer_id=7, awaited_by_scheduler=False)
    with (
        patch("backend.app.services.background_dispatch.printer_manager") as pm,
        patch.object(service, "_refuse_dispatch", new=AsyncMock()) as refuse,
        patch("backend.app.services.farm_startup_guard.farm_startup_guard.require"),
    ):
        pm.tray_layout.return_value = H3C
        await service._process_job(job)
    refuse.assert_awaited_once_with(job, direct_rack_gate.UNSUPPORTED_MESSAGE)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_autoqueue_does_not_route_to_a_rack(db_session, printer_factory):
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
    assert result.printer is None
    refused = [f for f in result.facts if f.printer_id == p.id]
    assert [f.code for f in refused] == ["direct_rack_unsupported"]
    assert routing_detail("direct_rack_unsupported")["message"] in refused[0].message


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
    assert response.status_code == 200, response.text
    assert response.json()["pending_config"] is True
    beyond = await async_client.post(
        "/api/v1/inventory/assignments",
        json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 32, "tray_id": 9},
    )
    assert beyond.status_code == 422
