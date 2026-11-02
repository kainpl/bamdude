"""GH106: durable configuration, source authority and safe queue placement."""

import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.virtual_printer import VirtualPrinter
from backend.app.services.bambu_mqtt import BambuMQTTClient, NozzleInfo, _parse_nozzle_type
from backend.app.services.printer_manager import printer_manager
from backend.app.services.virtual_printer.manager import VirtualPrinterInstance, VirtualPrinterManager
from backend.app.services.virtual_printer.mqtt_server import SimpleMQTTServer

pytestmark = pytest.mark.integration


def server(**kwargs):
    result = SimpleMQTTServer(
        serial="SYNTHETIC",
        access_code="00000000",
        cert_path=Path("unused.crt"),
        key_path=Path("unused.key"),
        **kwargs,
    )
    result._publish_to_report = AsyncMock()
    return result


async def report(mqtt):
    await mqtt._send_status_report(MagicMock())
    return mqtt._publish_to_report.call_args.args[1]["print"]


@pytest.mark.parametrize("diameter", ["0.2", "0.4", "0.6", "0.8"])
@pytest.mark.parametrize(
    "flow,letter", [("standard", "S"), ("high_flow", "H"), ("tpu_high_flow", "U"), ("e3d_high_flow", "B")]
)
async def test_unbound_reports_selected_main_nozzle(diameter, flow, letter):
    mqtt = server(nozzle_diameter=diameter, nozzle_flow=flow)
    payload = await report(mqtt)
    assert payload["nozzle_diameter"] == diameter
    assert payload["nozzle_type"] == f"H{letter}01"
    assert _parse_nozzle_type(payload["nozzle_type"]) == ("hardened_steel", flow)
    assert not ({"flag3", "device", "cfg", "fun", "aux", "stat"} & payload.keys())


async def test_unknown_stored_flow_reports_standard_without_breaking_status():
    payload = await report(server(nozzle_diameter="0.6", nozzle_flow="legacy_value"))
    assert (payload["nozzle_diameter"], payload["nozzle_type"]) == ("0.6", "HS01")


@pytest.mark.parametrize("cache", [None, {"command": "push_status", "gcode_state": "IDLE"}])
async def test_cold_bound_report_explicitly_clears_previous_manual_nozzle(cache):
    mqtt = server(nozzle_diameter="0.8", nozzle_flow="high_flow", has_target=True)
    bridge = MagicMock()
    bridge.get_latest_print_state.return_value = cache
    mqtt.set_bridge(bridge)
    payload = await report(mqtt)
    assert (payload["nozzle_diameter"], payload["nozzle_type"]) == ("0.0", "N/A")
    mqtt.set_bridge(None)  # Binding remains even if the bridge isn't available.
    assert (await report(mqtt))["nozzle_type"] == "N/A"


@pytest.mark.parametrize("shape", ["legacy", "modern", "both"])
async def test_real_nozzles_win_even_offline_and_cache_is_unchanged(shape):
    mqtt = server(nozzle_diameter="0.8", nozzle_flow="high_flow", has_target=True)
    cache = {
        "nozzle_diameter": "0.6",
        "nozzle_type": "HS01",
        "device": {
            "nozzle": {
                "info": [{"id": 0, "diameter": 0.6, "type": "HS01"}, {"id": 1, "diameter": 0.2, "type": "HH01"}],
            }
        },
    }
    if shape == "legacy":
        del cache["device"]
    if shape == "modern":
        del cache["nozzle_diameter"]
        del cache["nozzle_type"]
    original = copy.deepcopy(cache)
    bridge = MagicMock()
    bridge.is_active = False
    bridge.get_latest_print_state.return_value = cache
    mqtt.set_bridge(bridge)
    payload = await report(mqtt)
    if shape == "modern":
        assert "nozzle_diameter" not in payload and "nozzle_type" not in payload
    else:
        assert (payload["nozzle_diameter"], payload["nozzle_type"]) == ("0.6", "HS01")
    if shape != "legacy":
        assert payload["device"] == original["device"]
    assert cache == original


async def test_http_configuration_round_trip_and_null_rejection(async_client):
    response = await async_client.post("/api/v1/virtual-printers", json={"name": "Nozzle VP"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert (data["nozzle_diameter"], data["nozzle_flow"]) == ("0.4", "standard")
    url = f"/api/v1/virtual-printers/{data['id']}"
    response = await async_client.put(url, json={"nozzle_diameter": "0.6", "nozzle_flow": "high_flow"})
    assert response.status_code == 200
    await async_client.put(url, json={"name": "Renamed"})
    for invalid in [
        {"nozzle_diameter": None},
        {"nozzle_flow": None},
        {"nozzle_diameter": "0.5"},
        {"nozzle_flow": "fast"},
    ]:
        response = await async_client.put(url, json={"name": "Must not persist", **invalid})
        assert response.status_code == 422, response.text
    data = (await async_client.get(url)).json()
    assert (data["name"], data["nozzle_diameter"], data["nozzle_flow"]) == ("Renamed", "0.6", "high_flow")
    assert (await async_client.get("/api/v1/virtual-printers")).json()["printers"][0]["nozzle_flow"] == "high_flow"


async def test_sync_hot_updates_both_fields_without_dropping_sessions(db_session, test_engine, tmp_path):
    vp = VirtualPrinter(
        name="Hot",
        mode="file_manager",
        enabled=True,
        model="C11",
        access_code="00000000",
        nozzle_diameter="0.6",
        nozzle_flow="e3d_high_flow",
    )
    db_session.add(vp)
    await db_session.commit()
    manager = VirtualPrinterManager()
    manager._base_dir = tmp_path
    manager._session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
    inst = VirtualPrinterInstance(
        vp_id=vp.id,
        name=vp.name,
        mode=vp.mode,
        model=vp.model,
        access_code=vp.access_code,
        serial_suffix=vp.serial_suffix,
        base_dir=tmp_path,
    )
    inst._mqtt = server()
    inst.stop_server = AsyncMock()
    manager._instances[vp.id] = inst
    await manager.sync_from_db()
    assert manager._instances[vp.id] is inst
    inst.stop_server.assert_not_awaited()
    assert (await report(inst._mqtt))["nozzle_type"] == "HB01"
    assert (await report(inst._mqtt))["nozzle_diameter"] == "0.6"
    other = server(nozzle_diameter="0.2")
    assert (await report(other))["nozzle_diameter"] == "0.2"


@pytest.mark.parametrize("mode", ["file_manager", "proxy", "auto_queue"])
async def test_delete_target_cannot_rebind_vp_to_reused_printer_id(
    async_client, db_session, test_engine, tmp_path, printer_factory, monkeypatch, mode
):
    from backend.app.services.virtual_printer import virtual_printer_manager

    target = await printer_factory(name="Old physical printer")
    target_id = target.id
    vp = VirtualPrinter(name="Bound", mode=mode, enabled=True, target_printer_id=target_id)
    db_session.add(vp)
    await db_session.commit()
    vp_id = vp.id
    sync = AsyncMock()
    monkeypatch.setattr(virtual_printer_manager, "sync_from_db", sync)
    response = await async_client.delete(f"/api/v1/printers/{target_id}?delete_archives=false")
    assert response.status_code == 200, response.text
    replacement = await printer_factory(name="Different physical printer")
    if test_engine.dialect.name == "sqlite":
        assert replacement.id == target_id  # Actual reuse, not a manufactured fixture.
    bound_id = await db_session.scalar(select(VirtualPrinter.target_printer_id).where(VirtualPrinter.id == vp_id))
    assert bound_id is None
    sync.assert_awaited_once()
    if mode == "proxy":
        manager = VirtualPrinterManager()
        manager._base_dir = tmp_path
        manager._session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
        old = VirtualPrinterInstance(
            vp_id=vp_id,
            name="Bound",
            mode="proxy",
            model="BL-P001",
            access_code="",
            serial_suffix=vp.serial_suffix,
            target_printer_id=target_id,
            base_dir=tmp_path,
        )
        old.stop_proxy = AsyncMock()
        manager._instances[vp_id] = old
        await manager.sync_from_db()
        old.stop_proxy.assert_awaited_once()
        assert vp_id not in manager._instances


async def test_delete_cleans_id_caches_before_a_failing_vp_sync(
    async_client, db_session, printer_factory, monkeypatch, caplog
):
    import backend.app.api.routes.printers as printers_route
    from backend.app.models.printer import Printer
    from backend.app.services import ams_advertised_overlay, camera_metrics
    from backend.app.services.virtual_printer import virtual_printer_manager

    target = await printer_factory()
    target_id = target.id
    cleaned = []
    monkeypatch.setattr(camera_metrics, "forget_printer", lambda pid: cleaned.append(("camera", pid)))
    monkeypatch.setattr(ams_advertised_overlay, "forget_printer", lambda pid: cleaned.append(("overlay", pid)))
    monkeypatch.setattr(printers_route, "forget_printer_rebuild", lambda pid: cleaned.append(("rebuild", pid)))

    async def fail_sync():
        cleaned.append(("sync", target_id))
        raise RuntimeError("Synthetic VP sync failure")

    sync = AsyncMock(side_effect=fail_sync)
    monkeypatch.setattr(virtual_printer_manager, "sync_from_db", sync)
    response = await async_client.delete(f"/api/v1/printers/{target_id}?delete_archives=false")
    assert response.status_code == 200, response.text
    assert await db_session.scalar(select(Printer.id).where(Printer.id == target_id)) is None
    sync.assert_awaited_once()
    assert cleaned == [(kind, target_id) for kind in ("camera", "overlay", "rebuild", "sync")]
    assert "Failed to sync virtual printers after deleting printer" in caplog.text


@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("required,expected", [("0.6", True), ("0.8", False), ("0.4", True)])
async def test_queue_selection_uses_h2c_feed_snapshot(
    db_session, printer_factory, tmp_path, monkeypatch, bound, required, expected
):
    printer = await printer_factory(model="H2C")
    db_session.add(PrinterQueue(printer_id=printer.id))
    await db_session.commit()
    mqtt = BambuMQTTClient("127.0.0.1", "SYNTHETICH2C", "00000000", model="H2C")
    mqtt._client = MagicMock()
    mqtt.state.connected = True
    mqtt.state.state = "IDLE"
    mqtt._process_message(
        {
            "print": {
                "command": "push_status",
                "device": {
                    "nozzle": {
                        "info": [
                            {"id": 0, "diameter": 0.8, "type": "HS01", "sn": "N/A"},
                            {"id": 1, "diameter": 0.4, "type": "HS01", "sn": "A1", "tm": 300},
                            {"id": 16, "diameter": 0.6, "type": "HS01", "sn": "R0", "tm": 300},
                        ]
                    }
                },
            }
        }
    )
    monkeypatch.setitem(printer_manager._clients, printer.id, mqtt)
    monkeypatch.setitem(printer_manager._models, printer.id, "H2C")
    snapshot = printer_manager.get_feed_snapshot(printer.id)
    assert snapshot.nozzle_diameters == {0: (0.6,), 1: (0.4,)}
    # The mounted state still carries the absent carriage's stale diameter.
    assert mqtt.state.nozzles[0].nozzle_diameter == "0.8"
    inst = VirtualPrinterInstance(
        vp_id=1,
        name="Rack",
        mode="print_queue",
        model="O1C",
        access_code="00000000",
        serial_suffix="000000001",
        base_dir=tmp_path,
        target_printer_id=printer.id if bound else None,
    )
    selected = await inst._find_best_queue(db_session, "H2C", required)
    assert (selected is not None) is expected
    if expected:
        assert selected.printer_id == printer.id


@pytest.mark.parametrize("target_mode", ["unbound", "online", "offline"])
@pytest.mark.parametrize("reported,expected", [("0.4", 2), ("0.6", 1), ("", 1)])
async def test_queue_selection_skips_only_positive_diameter_mismatch(
    db_session, printer_factory, tmp_path, monkeypatch, target_mode, reported, expected
):
    first = await printer_factory(model="P1S")
    second = await printer_factory(model="P1S")
    db_session.add_all([PrinterQueue(printer_id=first.id), PrinterQueue(printer_id=second.id)])
    await db_session.commit()
    states = {
        first.id: SimpleNamespace(
            connected=target_mode != "offline", remaining_time=0, nozzles=[NozzleInfo(nozzle_diameter=reported)]
        ),
        second.id: SimpleNamespace(connected=True, remaining_time=10, nozzles=[NozzleInfo(nozzle_diameter="0.6")]),
    }
    monkeypatch.setattr(printer_manager, "get_status", states.get)
    monkeypatch.setattr(printer_manager, "effective_model_for", lambda _id, model=None: model or "P1S")
    inst = VirtualPrinterInstance(
        vp_id=1,
        name="Route",
        mode="print_queue",
        model="C12",
        access_code="00000000",
        serial_suffix="000000001",
        base_dir=tmp_path,
        target_printer_id=first.id if target_mode != "unbound" else None,
    )
    selected = await inst._find_best_queue(db_session, "P1S", "0.6")
    expected_id = second.id if target_mode == "offline" or expected == 2 else first.id
    assert selected.printer_id == expected_id
    states[second.id].nozzles[0].nozzle_diameter = "0.4"
    states[first.id].nozzles[0].nozzle_diameter = "0.4"
    assert await inst._find_best_queue(db_session, "P1S", "0.6") is None


async def test_intake_uses_source_diameter_and_keeps_unplaceable_file_in_library(
    db_session, test_engine, printer_factory, tmp_path, monkeypatch
):
    from backend.app.models.library import LibraryFile
    from backend.app.models.print_queue import PrintQueueItem
    from backend.tests.integration.test_filament_routing_dispatch import setup_source

    source, printer, _, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    source.file_metadata = {"sliced_for_model": "P1P", "nozzle_diameter": 0.6}
    await db_session.commit()
    mqtt._process_message({"print": {"command": "push_status", "nozzle_diameter": "0.4", "nozzle_type": "HS01"}})
    inst = VirtualPrinterInstance(
        vp_id=1,
        name="Intake",
        mode="print_queue",
        model="C11",
        access_code="00000000",
        serial_suffix="000000001",
        base_dir=tmp_path,
        nozzle_diameter="0.4",
        target_printer_id=printer.id,
        session_factory=async_sessionmaker(test_engine, expire_on_commit=False),
    )
    monkeypatch.setattr(inst, "_save_to_library", AsyncMock(return_value=source))
    await inst._add_to_print_queue(Path(source.file_path), "127.0.0.1")
    assert (await db_session.execute(select(PrintQueueItem))).scalar_one_or_none() is None
    assert await db_session.get(LibraryFile, source.id) is not None
    mqtt._client.publish.assert_not_called()


async def test_migration_backfills_existing_rows_and_is_repeatable(test_engine):
    from backend.app.migrations.m213_virtual_printer_nozzle import upgrade

    async with test_engine.begin() as conn:
        # Isolated temporary table avoids changing the app model metadata.
        await conn.execute(text("DROP TABLE virtual_printers"))
        await conn.execute(text("CREATE TABLE virtual_printers (id INTEGER PRIMARY KEY, name TEXT)"))
        await conn.execute(text("INSERT INTO virtual_printers (id, name) VALUES (1, 'Existing')"))
        await upgrade(conn)
        await upgrade(conn)
        row = (await conn.execute(text("SELECT nozzle_diameter, nozzle_flow FROM virtual_printers"))).one()
        assert tuple(row) == ("0.4", "standard")


async def test_rejected_delete_rolls_back_binding(async_client, db_session, printer_factory, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    target = await printer_factory()
    vp = VirtualPrinter(name="Still bound", target_printer_id=target.id)
    db_session.add(vp)
    await db_session.commit()
    target_id, vp_id = target.id, vp.id
    original = AsyncSession.delete

    async def fail_delete(session, obj):
        from fastapi import HTTPException

        from backend.app.models.printer import Printer

        if isinstance(obj, Printer):
            raise HTTPException(409, "Synthetic delete refusal")
        await original(session, obj)

    monkeypatch.setattr(AsyncSession, "delete", fail_delete)
    response = await async_client.delete(f"/api/v1/printers/{target_id}?delete_archives=false")
    assert response.status_code == 409
    assert (
        await db_session.scalar(select(VirtualPrinter.target_printer_id).where(VirtualPrinter.id == vp_id)) == target_id
    )


async def test_portable_backup_keeps_the_saved_pair(tmp_path):
    import sqlite3

    from sqlalchemy.ext.asyncio import create_async_engine

    from backend.app.core.database import Base
    from backend.app.core.db_portable import dump_to_sqlite

    source = tmp_path / "source.sqlite"
    backup = tmp_path / "backup.sqlite"
    engine = create_async_engine(f"sqlite+aiosqlite:///{source}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            db.add(VirtualPrinter(name="Portable", nozzle_diameter="0.8", nozzle_flow="tpu_high_flow"))
            await db.commit()
        await dump_to_sqlite(engine, Base.metadata, backup)
        with sqlite3.connect(backup) as db:
            assert db.execute("SELECT nozzle_diameter, nozzle_flow FROM virtual_printers").fetchone() == (
                "0.8",
                "tpu_high_flow",
            )
    finally:
        await engine.dispose()
