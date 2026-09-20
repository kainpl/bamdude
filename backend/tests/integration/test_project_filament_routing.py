"""Real project queue writers retain the selected plate before any dispatch."""

import pytest
from sqlalchemy import func, select

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.product import Product, ProductPart, ProductPlate
from backend.tests.fixtures.filament_routing_cases import write_routing_3mf

pytestmark = pytest.mark.integration

FILAMENT = [{"id": 3, "type": "PLA", "color": "#FF0000", "used_g": "4.25"}]


async def make_project(client, db, path, index):
    file = LibraryFile(
        filename=path.name,
        file_path=str(path),
        file_size=path.stat().st_size,
        file_type="gcode",
        file_metadata={
            "has_gcode": True,
            "sliced_for_model": "P1P",
            "plates": [{"index": index, "printable_objects": {"1": "part"}, "print_time_seconds": 3600}],
        },
    )
    product = Product(name="Routing part")
    db.add_all([file, product])
    await db.flush()
    plate = ProductPlate(product_id=product.id, library_file_id=file.id, plate_index=0)
    db.add_all(
        [plate, ProductPart(product_id=product.id, kind="printed", name="part", name_key="part", qty_per_unit=1)]
    )
    await db.commit()
    response = await client.post(
        "/api/v1/projects/",
        json={
            "name": "Routing order",
            "lines": [{"product_id": product.id, "quantity": 10, "material": "PLA"}],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return file.id, plate.id, body["id"], body["lines"][0]["id"]


async def target_for(db, kind):
    if kind == "auto":
        return {"kind": "auto"}
    printer = Printer(
        name="No AMS P1P", serial_number="ROUTING", ip_address="127.0.0.1", access_code="00000000", model="P1P"
    )
    db.add(printer)
    await db.flush()
    db.add(PrinterQueue(printer_id=printer.id))
    await db.commit()
    return {"kind": "printer", "printer_id": printer.id}


@pytest.mark.parametrize("index", [1, 2, 3, 4, 15])
@pytest.mark.parametrize("kind", ["auto", "printer"])
async def test_project_whole_file_retains_actual_plate_and_line(committing_client, db_session, tmp_path, index, kind):
    source = write_routing_3mf(tmp_path / "part.gcode.3mf", {index: FILAMENT})
    file_id, recipe_id, project_id, line_id = await make_project(committing_client, db_session, source, index)
    response = await committing_client.post(
        f"/api/v1/projects/{project_id}/plan/enqueue",
        json={
            "items": [{"plate_id": recipe_id, "line_id": line_id, "count": 3}],
            "target": await target_for(db_session, kind),
        },
    )
    assert response.status_code == 200, response.text
    ids = response.json()["created"][0]["queue_item_ids"]
    model = AutoQueueItem if kind == "auto" else PrintQueueItem
    rows = (await db_session.execute(select(model).where(model.id.in_(ids)))).scalars().all()
    assert len(rows) == 3
    assert {(r.plate_id, r.library_file_id, r.project_line_id, r.status) for r in rows} == {
        (index, file_id, line_id, "pending")
    }
    assert (await db_session.get(ProductPlate, recipe_id)).plate_index == 0
    if kind == "auto":
        assert {r.required_filament_types for r in rows} == {'["PLA"]'}
        assert {r.target_model for r in rows} == {"P1P"}


@pytest.mark.parametrize("kind", ["auto", "printer"])
async def test_project_ambiguous_source_refuses_all_rows_before_any_writer(
    committing_client, db_session, tmp_path, kind
):
    source = write_routing_3mf(tmp_path / "multi.gcode.3mf", {1: FILAMENT, 4: FILAMENT})
    file_id, recipe_id, project_id, line_id = await make_project(committing_client, db_session, source, 1)
    valid_plate = ProductPlate(
        product_id=(await db_session.get(ProductPlate, recipe_id)).product_id, library_file_id=file_id, plate_index=1
    )
    db_session.add(valid_plate)
    await db_session.commit()
    response = await committing_client.post(
        f"/api/v1/projects/{project_id}/plan/enqueue",
        json={
            "items": [
                {"plate_id": valid_plate.id, "line_id": line_id, "count": 2},
                {"plate_id": recipe_id, "line_id": line_id, "count": 2},
            ],
            "target": await target_for(db_session, kind),
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "plate_selection_required"
    assert response.json()["detail"]["params"]["product_plate_id"] == recipe_id
    for model in (AutoQueueItem, PrintQueueItem):
        assert (await db_session.execute(select(func.count()).select_from(model))).scalar() == 0


async def test_five_external_p1p_project_jobs_produce_five_safe_commands(
    committing_client, db_session, test_engine, tmp_path, printer_factory, monkeypatch
):
    import json
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock, MagicMock

    from backend.app.services.auto_queue_scheduler import AutoQueueScheduler
    from backend.app.services.bambu_mqtt import BambuMQTTClient
    from backend.app.services.printer_manager import printer_manager

    path = write_routing_3mf(
        tmp_path / "black-petg.gcode.3mf",
        {
            4: [
                {"id": 1, "type": "PETG", "color": "#000000", "used_g": "5"},
            ]
        },
    )
    _, recipe, project, line = await make_project(committing_client, db_session, path, 4)
    clients = {}
    for index in range(5):
        printer = await printer_factory(model="P1P", serial_number=f"SYNTHETIC{index}")
        db_session.add(PrinterQueue(id=printer.id, printer_id=printer.id, auto_distribute_eligible=True))
        mqtt = BambuMQTTClient("127.0.0.1", printer.serial_number, "00000000", model="C11")
        mqtt._client = MagicMock()
        mqtt.state.connected, mqtt.state.state = True, "IDLE"
        mqtt._process_message(
            {"print": {"ams": {"ams": []}, "vt_tray": {"id": 254, "tray_type": "PETG", "tray_color": "000000"}}}
        )
        monkeypatch.setitem(printer_manager._clients, printer.id, mqtt)
        monkeypatch.setitem(printer_manager._models, printer.id, "P1P")
        clients[printer.id] = mqtt
    await db_session.commit()
    response = await committing_client.post(
        f"/api/v1/projects/{project}/plan/enqueue",
        json={
            "items": [{"plate_id": recipe, "line_id": line, "count": 5}],
            "target": {"kind": "auto"},
        },
    )
    assert response.status_code == 200, response.text

    @asynccontextmanager
    async def session():
        yield db_session

    monkeypatch.setattr("backend.app.services.auto_queue_scheduler.async_session", session)
    await AutoQueueScheduler().tick()
    jobs = (await db_session.execute(select(PrintQueueItem))).scalars().all()
    assert len(jobs) == 5 and len({job.queue_id for job in jobs}) == 5
    from sqlalchemy.ext.asyncio import async_sessionmaker

    import backend.app.services.background_dispatch as bd
    import backend.app.services.print_scheduler as ps
    from backend.app.core.config import settings

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr(settings, "base_dir", tmp_path)
    monkeypatch.setattr(settings, "archive_dir", tmp_path / "archives")
    settings.archive_dir.mkdir()
    monkeypatch.setattr(ps, "async_session", factory)
    monkeypatch.setattr(bd, "async_session", factory)
    monkeypatch.setattr(bd, "resolve_dispatch_storage", lambda *_: ("external", None))
    monkeypatch.setattr(bd, "upload_file_async", AsyncMock(return_value=True))
    monkeypatch.setattr(bd, "delete_file_async", AsyncMock(return_value=True))
    monkeypatch.setattr(bd, "list_files_async", AsyncMock(return_value=[]))
    monkeypatch.setattr(bd, "get_ftp_retry_settings", AsyncMock(return_value=(False, 0, 0, 30)))
    monkeypatch.setattr(bd.printer_manager, "ensure_fresh_connection_for_printer", AsyncMock(return_value=True))
    monkeypatch.setattr(bd, "_warn_on_filament_deficit", AsyncMock())
    monkeypatch.setattr(bd, "_apply_calibrations_for_print", AsyncMock())
    monkeypatch.setattr("backend.app.services.preheat.preheat_and_soak", AsyncMock())
    monkeypatch.setattr("backend.app.main.register_expected_print", MagicMock())
    monkeypatch.setattr(bd.ws_manager, "broadcast", AsyncMock())
    service = bd.BackgroundDispatchService()
    monkeypatch.setattr(bd, "background_dispatch", service)
    monkeypatch.setattr(service, "_ensure_live_connection_before_start", AsyncMock())
    monkeypatch.setattr(service, "_run_swap_macro_if_needed", AsyncMock())
    monkeypatch.setattr(service, "_verify_print_response", AsyncMock(return_value=True))
    monkeypatch.setattr(ps.scheduler, "acquire_stagger_slot", AsyncMock())
    scheduler = ps.PrintScheduler()
    monkeypatch.setattr(scheduler, "_check_auto_drying", AsyncMock())
    monkeypatch.setattr(
        scheduler, "_watchdog_print_start", AsyncMock()
    )  # Firmware acknowledgement is outside this offline test.
    spawned = []
    monkeypatch.setattr(ps, "spawn_background_task", lambda coroutine, **kw: spawned.append(coroutine))
    import asyncio

    assert await asyncio.wait_for(scheduler.check_queue(), timeout=15)
    assert len(spawned) == 5
    for coroutine in spawned:
        await asyncio.wait_for(coroutine, timeout=15)
    for job in jobs:
        command = json.loads(clients[job.queue_id]._client.publish.call_args.args[1])["print"]
        assert command["use_ams"] is False and command["ams_mapping"] == [0]
        assert command["param"] == "Metadata/plate_4.gcode"
        assert "ams_mapping2" not in command
    assert sum(mqtt._client.publish.call_count for mqtt in clients.values()) == 5
