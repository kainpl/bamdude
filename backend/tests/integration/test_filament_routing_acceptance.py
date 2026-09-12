"""Cross-boundary acceptance cases beyond the parser and pure matcher."""

from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import select

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services.auto_queue_scheduler import AutoQueueScheduler
from backend.app.services.filament_preflight import preflight_item
from backend.tests.integration.test_filament_routing_dispatch import setup_source

pytestmark = pytest.mark.integration


async def test_unreadable_head_does_not_block_later_valid_auto_job(
    committing_client,
    db_session,
    tmp_path,
    printer_factory,
    monkeypatch,
):
    source, printer, _, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    broken = AutoQueueItem(target_model="P1P", position=0, status="pending")
    db_session.add(broken)
    await db_session.commit()
    created = await committing_client.post("/api/v1/auto-queue/", json={"library_file_id": source.id})
    assert created.status_code == 200

    @asynccontextmanager
    async def session():
        yield db_session

    monkeypatch.setattr("backend.app.services.auto_queue_scheduler.async_session", session)
    await AutoQueueScheduler().tick()
    queued = (await db_session.execute(select(PrintQueueItem))).scalar_one()
    assert queued.source_auto_item_id == created.json()["id"]
    await db_session.refresh(broken)
    # An item whose source cannot be read is FAILED with the reason on the row
    # (since 2026-09-11, "fail unavailable sources without blocking remaining
    # jobs") — it no longer waits forever as ``pending``. The point of this test
    # is the line above: the valid job behind it still gets placed.
    assert broken.status == "failed" and broken.waiting_reason
    mqtt._client.publish.assert_not_called()


async def test_only_raw_gcode_or_server_calibration_is_exempt_from_normal_preflight(
    db_session,
    tmp_path,
    printer_factory,
    monkeypatch,
):
    from backend.app.services.filament_routing import RoutingDeferred

    source, printer, queue, _ = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    row = PrintQueueItem(queue_id=queue.id, library_file_id=source.id)
    db_session.add(row)
    await db_session.commit()
    Path(source.file_path).write_bytes(b"corrupt ordinary 3mf")
    with pytest.raises(RoutingDeferred):
        await preflight_item(db_session, row, printer.id)
    raw = tmp_path / "part.gcode"
    raw.write_text("G28\n")
    source.file_path = str(raw)
    await db_session.commit()
    assert await preflight_item(db_session, row, printer.id) is None
    source.file_path = str(tmp_path / "missing.3mf")
    row.is_calibration = True
    row.calibration_session_id = 9  # Server-created calibration context; preflight is read-only.
    assert await preflight_item(db_session, row, printer.id) is None


@pytest.mark.parametrize("material", ["PETG", "PVA"])
def test_relaxed_body_with_pinned_support_still_requires_both_materials(material):
    from backend.app.services.filament_routing import RoutingPolicy, resolve_filament_routing
    from backend.tests.unit.services.test_filament_routing import feed, requirements, snapshot

    req = requirements({"type": "PLA"}, {"type": material, "color": "#00FF00"})
    policy = RoutingPolicy(filament_overrides=({"slot_id": 2, "force_color_match": True},))
    sources = snapshot(feed(0, "0000FF", kind="ams"), feed(1, "00FF00", kind="ams", material=material))
    result = resolve_filament_routing(req, policy, sources)
    assert result.plan.mapping == [0, 1]
    wrong = snapshot(feed(0, "0000FF", kind="ams"), feed(1, "00FF00", kind="ams", material="PLA"))
    assert resolve_filament_routing(req, policy, wrong).plan is None


async def test_incompatible_first_printer_is_skipped_for_a_compatible_candidate(
    committing_client,
    db_session,
    tmp_path,
    printer_factory,
    monkeypatch,
):
    from unittest.mock import MagicMock

    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.services.bambu_mqtt import BambuMQTTClient
    from backend.app.services.printer_manager import printer_manager

    source, first, _, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    mqtt._process_message({"print": {"vt_tray": {"tray_type": "PETG"}}})
    second = await printer_factory(model="P1P")
    db_session.add(PrinterQueue(id=second.id, printer_id=second.id, auto_distribute_eligible=True))
    await db_session.commit()
    ready = BambuMQTTClient("127.0.0.1", "SYNTHETIC2", "00000000", model="C11")
    ready._client = MagicMock()
    ready.state.connected, ready.state.state = True, "IDLE"
    ready._process_message(
        {"print": {"ams": {"ams": []}, "vt_tray": {"id": 254, "tray_type": "PLA", "tray_color": "FF0000"}}}
    )
    monkeypatch.setitem(printer_manager._clients, second.id, ready)
    monkeypatch.setitem(printer_manager._models, second.id, "P1P")
    created = await committing_client.post(
        "/api/v1/auto-queue/", json={"library_file_id": source.id, "force_color_match": True}
    )
    assert created.status_code == 200

    @asynccontextmanager
    async def session():
        yield db_session

    monkeypatch.setattr("backend.app.services.auto_queue_scheduler.async_session", session)
    await AutoQueueScheduler().tick()
    row = (await db_session.execute(select(PrintQueueItem))).scalar_one()
    assert row.queue_id == second.id and row.queue_id != first.id
    guard = await preflight_item(db_session, row, second.id)
    assert guard.plan.mapping == [-1, -1, 254] and guard.policy.force_color_match
    assert printer_manager.start_print(
        second.id,
        source.filename,
        row.plate_id,
        ams_mapping=guard.plan.mapping,
        use_ams=guard.plan.use_ams,
        routing_guard=guard,
    )
    mqtt._client.publish.assert_not_called()
    ready._client.publish.assert_called_once()


def test_body_color_pin_keeps_relaxed_support_material_and_wrong_nozzle_is_refused():
    from backend.app.services.filament_routing import RoutingPolicy, resolve_filament_routing
    from backend.tests.unit.services.test_filament_routing import feed, requirements, snapshot

    policy = RoutingPolicy(filament_overrides=({"slot_id": 1, "force_color_match": True},))
    req = requirements({}, {"type": "PVA", "color": "#FFFFFF"})
    sources = snapshot(feed(0, "FF0000", kind="ams"), feed(1, "00FF00", kind="ams", material="PVA"))
    assert resolve_filament_routing(req, policy, sources).plan.mapping == [0, 1]
    wrong = snapshot(feed(0, "0000FF", kind="ams"), feed(1, "00FF00", kind="ams", material="PVA"))
    assert resolve_filament_routing(req, policy, wrong).plan is None
    dual = requirements({"nozzle_id": 0}, model="X2D")
    wrong_side = snapshot(feed(0, kind="ams", nozzle=1), model="X2D")
    assert resolve_filament_routing(dual, RoutingPolicy(), wrong_side).reason == "nozzle_mismatch"


async def test_preflight_honors_lowest_setting_inventory_priority_and_backup_gate(
    db_session,
    tmp_path,
    printer_factory,
    monkeypatch,
):
    from unittest.mock import AsyncMock

    from backend.app.models.settings import Settings
    from backend.app.services.filament_policy_write import prepare_routing
    from backend.app.services.print_scheduler import scheduler

    source, printer, queue, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    mqtt._process_message(
        {
            "print": {
                "ams": {
                    "ams": [
                        {
                            "id": 0,
                            "tray": [
                                {"id": 0, "tray_type": "PLA", "tray_color": "FF0000", "remain": 80},
                                {"id": 1, "tray_type": "PLA", "tray_color": "FF0000", "remain": 5},
                            ],
                        }
                    ]
                },
                "vt_tray": [],
            }
        }
    )
    db_session.add(Settings(key="prefer_lowest_filament", value="true"))
    rules, plate = await prepare_routing(db_session, printer_id=printer.id, library_file_id=source.id)
    row = PrintQueueItem(queue_id=queue.id, library_file_id=source.id, plate_id=plate, filament_routing=rules)
    db_session.add(row)
    await db_session.commit()
    remaining = AsyncMock(return_value={})
    monkeypatch.setattr(scheduler, "_build_inventory_remain_overrides", remaining)
    assert (await preflight_item(db_session, row, printer.id)).plan.mapping == [-1, -1, 1]
    remaining.return_value = {0: 20.0}  # Inventory grams outrank MQTT-only percentages.
    assert (await preflight_item(db_session, row, printer.id)).plan.mapping == [-1, -1, 0]
    remaining.reset_mock()
    mqtt.state.ams_auto_switch_filament = False
    assert (await preflight_item(db_session, row, printer.id)).plan.mapping == [-1, -1, 0]
    remaining.assert_not_awaited()
