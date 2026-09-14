"""Synthetic files, real intake/distributor/preflight/MQTT serialization; no printer I/O."""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.user_filament import UserFilamentFamily
from backend.app.services.auto_queue_scheduler import AutoQueueScheduler
from backend.app.services.bambu_mqtt import BambuMQTTClient
from backend.app.services.filament_deferred import defer_claim
from backend.app.services.filament_policy import deserialize_policy
from backend.app.services.filament_preflight import final_guard, preflight_item
from backend.app.services.filament_routing import RoutingDeferred
from backend.app.services.printer_manager import printer_manager
from backend.tests.fixtures.filament_routing_cases import write_routing_3mf

pytestmark = pytest.mark.integration


async def setup_source(db, tmp_path, printer_factory, monkeypatch):
    path = write_routing_3mf(
        tmp_path / "part.gcode.3mf",
        {
            15: [
                {"id": 3, "type": "PLA", "color": "#FF0000", "used_g": "0.0001"},
            ]
        },
    )
    source = LibraryFile(filename=path.name, file_path=str(path), file_size=path.stat().st_size, file_type="gcode")
    db.add(source)
    printer = await printer_factory(model="P1P")
    queue = PrinterQueue(id=printer.id, printer_id=printer.id, auto_distribute_eligible=True)
    db.add(queue)
    await db.commit()
    mqtt = BambuMQTTClient("127.0.0.1", "SYNTHETIC", "00000000", model="P1P")
    mqtt._client = MagicMock()
    mqtt.state.connected = True
    mqtt.state.state = "IDLE"
    mqtt._process_message(
        {
            "print": {
                "command": "push_status",
                "ams": {"ams": []},
                "vt_tray": {"id": 254, "tray_type": "PLA", "tray_color": "0000FF"},
            }
        }
    )
    monkeypatch.setitem(printer_manager._clients, printer.id, mqtt)
    monkeypatch.setitem(printer_manager._models, printer.id, "P1P")
    return source, printer, queue, mqtt


@pytest.mark.parametrize("force,expect_assignment", [(False, True), (True, False)])
async def test_auto_intake_tick_and_publish_sparse_external(
    committing_client, db_session, tmp_path, printer_factory, monkeypatch, force, expect_assignment
):
    source, printer, queue, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    response = await committing_client.post(
        "/api/v1/auto-queue/",
        json={
            "library_file_id": source.id,
            "force_color_match": force,
        },
    )
    assert response.status_code == 200, response.text

    @asynccontextmanager
    async def session():
        yield db_session

    monkeypatch.setattr("backend.app.services.auto_queue_scheduler.async_session", session)
    await AutoQueueScheduler().tick()
    item = (await db_session.execute(select(PrintQueueItem))).scalar_one_or_none()
    assert (item is not None) == expect_assignment
    if item is None:
        auto = (await db_session.execute(select(AutoQueueItem))).scalar_one()
        assert auto.waiting_reason
        mqtt._client.publish.assert_not_called()
        return
    assert item.plate_id == 15
    # m173: the promotion carries the captured source onto the per-printer row —
    # the assignment re-reads nothing from the share, and both rows own the blob
    # until the shared cleanup (queue-source-spool spec §7).
    auto_row = (await db_session.execute(select(AutoQueueItem))).scalar_one()
    assert auto_row.queue_source_id is not None
    assert item.queue_source_id == auto_row.queue_source_id
    assert item.source_snapshot == auto_row.source_snapshot
    assert not item.use_ams
    assert json.loads(item.ams_mapping) == [-1, -1, 254]
    assert deserialize_policy(item.filament_routing).mode == "auto"
    guard = await preflight_item(db_session, item, printer.id)
    guard = await final_guard(guard, printer.id)
    assert printer_manager.start_print(
        printer.id, source.filename, 15, ams_mapping=guard.plan.mapping, use_ams=guard.plan.use_ams, routing_guard=guard
    )
    command = json.loads(mqtt._client.publish.call_args.args[1])["print"]
    assert command["param"] == "Metadata/plate_15.gcode"
    assert command["use_ams"] is False
    assert command["ams_mapping"] == [0, 0, 0]  # existing no-AMS wire convention
    assert "ams_mapping2" not in command


async def test_publish_boundary_catches_change_after_final_preflight(
    db_session, tmp_path, printer_factory, monkeypatch
):
    from backend.app.services.filament_policy_write import prepare_routing

    source, printer, queue, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    routing, plate = await prepare_routing(db_session, printer_id=printer.id, library_file_id=source.id)
    item = PrintQueueItem(queue_id=queue.id, library_file_id=source.id, plate_id=plate, filament_routing=routing)
    db_session.add(item)
    await db_session.commit()
    guard = await final_guard(await preflight_item(db_session, item, printer.id), printer.id)
    mqtt._process_message({"print": {"vt_tray": {"id": 254, "tray_type": "PETG"}}})
    with pytest.raises(RoutingDeferred, match="feed_state_changed"):
        printer_manager.start_print(
            printer.id,
            source.filename,
            plate,
            ams_mapping=guard.plan.mapping,
            use_ams=guard.plan.use_ams,
            routing_guard=guard,
        )
    mqtt._client.publish.assert_not_called()


async def test_per_printer_routing_uses_the_profile_family_material_when_enabled(
    db_session, tmp_path, printer_factory, monkeypatch
):
    from backend.app.services.filament_policy_write import prepare_routing

    source, printer, queue, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    path = write_routing_3mf(
        tmp_path / source.filename,
        {15: [{"id": 3, "type": "333Print PETG", "tray_info_idx": "P333PETG", "used_g": "0.0001"}]},
    )
    source.file_path = str(path)
    db_session.add(
        UserFilamentFamily(
            filament_id="P333PETG",
            ecosystem="bambu",
            alias="333Print PETG",
            vendor="333Print",
            filament_type="PETG",
            origin="cloud_bambu",
        )
    )
    mqtt._process_message({"print": {"vt_tray": {"id": 254, "tray_type": "PETG", "tray_color": "FF0000"}}})
    await db_session.commit()

    routing, plate = await prepare_routing(
        db_session,
        printer_id=printer.id,
        library_file_id=source.id,
        options={"allow_base_material_match": True},
    )
    item = PrintQueueItem(queue_id=queue.id, library_file_id=source.id, plate_id=plate, filament_routing=routing)
    db_session.add(item)
    await db_session.commit()
    assert (await preflight_item(db_session, item, printer.id)).plan is not None


@pytest.mark.parametrize("race", ["none", "cancel", "reclaim", "delete"])
async def test_defer_cas_never_revives_another_attempt(db_session, printer_factory, race):
    printer = await printer_factory()
    queue = PrinterQueue(id=printer.id, printer_id=printer.id, status="printing")
    db_session.add(queue)
    await db_session.flush()
    started = datetime(2026, 9, 10, 10)
    item = PrintQueueItem(queue_id=queue.id, status="printing", started_at=started, archive_id=None)
    db_session.add(item)
    await db_session.flush()
    queue.current_item_id = item.id
    iid = item.id
    if race == "cancel":
        item.status = "cancelled"
    if race == "reclaim":
        item.started_at = started + timedelta(seconds=1)
    if race == "delete":
        queue.current_item_id = None
        await db_session.delete(item)
    await db_session.commit()
    restored = await defer_claim(db_session, item_id=iid, started_at=started, reason="feed_state_changed")
    await db_session.commit()
    assert restored == (race == "none")
    if race != "delete":
        await db_session.refresh(item)
        assert item.status == {"none": "pending", "cancel": "cancelled", "reclaim": "printing"}[race]


@pytest.mark.parametrize("model", ["H2D", "O1D", "H2D Pro", "H2DPRO", "H2C", "O1C", "X2D", "N6", "O1E", "O2D", "O1C2"])
@pytest.mark.parametrize("kind", ["mixed", "external"])
async def test_dual_topology_and_real_mqtt_wire(db_session, tmp_path, printer_factory, monkeypatch, model, kind):
    from backend.app.services.filament_policy_write import prepare_routing
    from backend.app.utils.printer_models import normalize_model_name
    from backend.tests.fixtures.filament_routing_cases import DUAL_SETTINGS, mixed_filaments

    source, printer, queue, _ = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    path = write_routing_3mf(tmp_path / "dual.3mf", {2: mixed_filaments()}, model=model, settings=DUAL_SETTINGS)
    source.file_path, source.filename, printer.model = str(path), path.name, normalize_model_name(model)
    await db_session.commit()
    mqtt = BambuMQTTClient("127.0.0.1", "DUAL_SYNTHETIC", "00000000", model=model)
    mqtt._client = MagicMock()
    mqtt.state.connected, mqtt.state.state = True, "IDLE"
    nozzle_ids = (1, 16) if normalize_model_name(model) == "H2C" else (0, 1)
    mqtt._process_message(
        {
            "print": {
                "ams": {
                    "ams": (
                        [{"id": 0, "info": "100", "tray": [{"id": 0, "tray_type": "PLA", "tray_color": "FF0000"}]}]
                        if kind == "mixed"
                        else []
                    )
                },
                "vir_slot": [
                    {"id": 254, "tray_type": "PLA", "tray_color": "FF0000"},
                    {"id": 255, "tray_type": "PETG", "tray_color": "00FF00"},
                ],
                "device": {
                    "nozzle": {
                        "info": [{"id": nozzle_ids[0], "diameter": "0.6"}, {"id": nozzle_ids[1], "diameter": "0.4"}]
                    }
                },
            }
        }
    )
    monkeypatch.setitem(printer_manager._clients, printer.id, mqtt)
    monkeypatch.setitem(printer_manager._models, printer.id, printer.model)
    routing, plate = await prepare_routing(
        db_session,
        printer_id=printer.id,
        library_file_id=source.id,
        options={"feed_policy": "auto" if kind == "mixed" else "external_only"},
    )
    item = PrintQueueItem(queue_id=queue.id, library_file_id=source.id, plate_id=plate, filament_routing=routing)
    db_session.add(item)
    await db_session.commit()
    guard = await final_guard(await preflight_item(db_session, item, printer.id), printer.id)
    assert guard.plan.mapping == ([0, 255] if kind == "mixed" else [254, 255])
    assert printer_manager.start_print(
        printer.id, path.name, plate, ams_mapping=guard.plan.mapping, use_ams=guard.plan.use_ams, routing_guard=guard
    )
    command = json.loads(mqtt._client.publish.call_args.args[1])["print"]
    assert command["use_ams"] is (kind == "mixed")
    assert command["ams_mapping"] == ([0, -1] if kind == "mixed" else [-1, -1])
    assert command["ams_mapping2"] == [
        {"ams_id": 0 if kind == "mixed" else 254, "slot_id": 0},
        {"ams_id": 255, "slot_id": 0},
    ]
    assert command["param"] == "Metadata/plate_2.gcode"
