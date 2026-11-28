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
