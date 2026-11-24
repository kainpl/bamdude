"""GH129: real 3MF and MQTT parsing through both routing previews."""

from unittest.mock import MagicMock

import pytest

from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.bambu_mqtt import BambuMQTTClient
from backend.app.services.filament_preflight import final_guard, preflight_item
from backend.app.services.printer_manager import printer_manager
from backend.tests.fixtures.filament_routing_cases import write_routing_3mf

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "requested,profile,reported,reported_profile,effective,compatible",
    [
        ("ABS-S", "GFS06", "ABS", "GFS06", "ABS-S", True),
        ("PLA-S", "GFS05", "PLA", "GFS05", "PLA-S", True),
        ("ABS-S", "GFS06", "ABS", "GFB00", "ABS", False),
        ("ABS", "GFB00", "ABS", "GFS06", "ABS-S", False),
    ],
)
async def test_x2d_support_material_on_right_external_holder(
    requested,
    profile,
    reported,
    reported_profile,
    effective,
    compatible,
    committing_client,
    db_session,
    tmp_path,
    printer_factory,
    monkeypatch,
):
    path = write_routing_3mf(
        tmp_path / "support.gcode.3mf",
        {
            1: [
                {"id": 1, "type": "PLA", "tray_info_idx": "GFA00", "used_g": "4", "group_id": 0},
                {"id": 2, "type": requested, "tray_info_idx": profile, "used_g": "1", "group_id": 1},
            ]
        },
        model="N6",
        settings={"physical_extruder_map": ["1", "0"], "nozzle_diameter": ["0.4", "0.4"]},
    )
    file = LibraryFile(filename=path.name, file_path=str(path), file_size=path.stat().st_size, file_type="gcode")
    printer = await printer_factory(model="X2D")
    queue = PrinterQueue(printer_id=printer.id, auto_distribute_eligible=True)
    db_session.add_all([file, queue])
    await db_session.commit()
    mqtt = BambuMQTTClient("127.0.0.1", "SYNTHETIC", "00000000", model="X2D")
    mqtt._client = MagicMock()
    mqtt.state.connected = True
    mqtt._process_message(
        {
            "print": {
                "gcode_state": "IDLE",
                "ams": {
                    "ams_exist_bits": "1",
                    "ams": [
                        {
                            "id": 0,
                            "info": "11001103",
                            "tray": [
                                {"id": 0, "tray_type": "PLA", "tray_info_idx": "GFA00"},
                                {"id": 1, "tray_type": "PETG", "tray_info_idx": "GFG00"},
                                {"id": 2, "tray_type": "ASA", "tray_info_idx": "GFB01"},
                                {"id": 3, "tray_type": "ASA", "tray_info_idx": "GFB01"},
                            ],
                        }
                    ],
                },
                "vir_slot": [{"id": 255, "tray_type": reported, "tray_info_idx": reported_profile}],
                "device": {"nozzle": {"info": [{"id": 0, "diameter": "0.4"}, {"id": 1, "diameter": "0.4"}]}},
            }
        }
    )
    monkeypatch.setitem(printer_manager._clients, printer.id, mqtt)
    monkeypatch.setitem(printer_manager._models, printer.id, "X2D")

    status = await committing_client.get(f"/api/v1/printers/{printer.id}/status")
    assert status.status_code == 200, status.text
    external = next(tray for tray in status.json()["vt_tray"] if tray["id"] == 255)
    assert (external["tray_type"], external["routing_type"], external["tray_info_idx"]) == (
        reported,
        effective,
        reported_profile,
    )

    for allow_base in (False, True):
        choices = {"library_file_id": file.id, "allow_base_material_match": allow_base}
        auto = await committing_client.post(
            "/api/v1/auto-queue/routing-preview",
            json={**choices, "plate_ids": [1]},
        )
        assert auto.status_code == 200, auto.text
        assert auto.json()["plates"][0]["groups"][0]["compatible"] == int(compatible)
        for pinned in (False, True):
            target = {"printer_id": printer.id, "plate_id": 1}
            if pinned:
                target.update(manual_mapping=True, ams_mapping=[0, 255])
            response = await committing_client.post(
                "/api/v1/auto-queue/printer-routing-preview",
                json={**choices, "targets": [target]},
            )
            assert response.status_code == 200, response.text
            result = response.json()["targets"][0]
            expected = "compatible" if compatible else "incompatible"
            assert result["status"] == expected
            assert result["mapping"] == ([0, 255] if compatible else None)
            if not compatible:
                assert f"Ext-R: {effective} ({reported_profile})" in result["reason"]["message"]
            assert result["dual_nozzle"]
            channel = next(row for row in result["channels"] if row["slot_id"] == 2)
            assert (channel["material"], channel["profile"]) == (requested, profile)
            assert channel["selected_source_id"] == (255 if pinned or compatible else None)
            # The right holder stays visible ahead of all four AMS slots.
            source = channel["sources"][0]
            assert (source["source_id"], source["kind"]) == (255, "external")
            assert (source["reported_material"], source["material"], source["profile"]) == (
                reported,
                effective,
                reported_profile,
            )
            assert source["status"] == expected
            assert (source["reason"] and source["reason"]["code"]) == (None if compatible else "material_mismatch")
    if compatible:
        queued = await committing_client.post(
            "/api/v1/queue/",
            json={
                "queue_id": queue.id,
                "library_file_id": file.id,
                "plate_id": 1,
                "allow_base_material_match": False,
                "manual_mapping": True,
                "ams_mapping": [0, 255],
            },
        )
        assert queued.status_code == 200, queued.text
        item = await db_session.get(PrintQueueItem, queued.json()["id"])
        guard = await preflight_item(db_session, item, printer.id)
        assert guard.plan.mapping == [0, 255]
        assert (await final_guard(guard, printer.id)).plan.mapping == [0, 255]
    mqtt._client.publish.assert_not_called()
