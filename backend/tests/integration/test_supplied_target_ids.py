"""Zero is a supplied reference; None/omission retain existing link semantics."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import delete, func, select

from backend.app.models.library import LibraryFolder
from backend.app.models.printer import Printer
from backend.app.models.smart_plug import SmartPlug
from backend.app.models.virtual_printer import VirtualPrinter

pytestmark = pytest.mark.asyncio


def plug_body(**changes):
    return {"name": "Target validation", "ip_address": "192.0.2.5", "enabled": False, **changes}


@pytest.mark.parametrize("target", [0, 999999])
async def test_plug_create_missing_supplied_printer_is_atomic(async_client, db_session, target):
    response = await async_client.post("/api/v1/smart-plugs/", json=plug_body(printer_id=target))
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "Printer not found"
    assert await db_session.scalar(select(func.count()).select_from(SmartPlug)) == 0


@pytest.mark.parametrize("selection", ["omitted", "null", "valid"])
async def test_plug_create_optional_and_valid_reference(async_client, printer_factory, selection):
    printer = await printer_factory()
    fields = {} if selection == "omitted" else {"printer_id": printer.id if selection == "valid" else None}
    response = await async_client.post("/api/v1/smart-plugs/", json=plug_body(**fields))
    assert response.status_code == 200, response.text
    assert response.json()["printer_id"] == (printer.id if selection == "valid" else None)


@pytest.mark.parametrize("target", [0, 999999])
async def test_plug_update_missing_target_preserves_binding_and_name(
    async_client, db_session, smart_plug_factory, printer_factory, target
):
    printer = await printer_factory()
    plug = await smart_plug_factory(printer_id=printer.id, enabled=False)
    old_name = plug.name
    response = await async_client.patch(
        f"/api/v1/smart-plugs/{plug.id}", json={"printer_id": target, "name": "must not be saved"}
    )
    assert response.status_code == 400, response.text
    await db_session.refresh(plug)
    assert (plug.printer_id, plug.name) == (printer.id, old_name)


@pytest.mark.parametrize("selection", ["omitted", "null", "valid"])
async def test_plug_update_keep_clear_and_rebind(async_client, smart_plug_factory, printer_factory, selection):
    first, second = await printer_factory(), await printer_factory()
    plug = await smart_plug_factory(printer_id=first.id, enabled=False)
    fields = {} if selection == "omitted" else {"printer_id": second.id if selection == "valid" else None}
    response = await async_client.patch(f"/api/v1/smart-plugs/{plug.id}", json={"name": "Renamed", **fields})
    assert response.status_code == 200, response.text
    expected = first.id if selection == "omitted" else second.id if selection == "valid" else None
    assert response.json()["printer_id"] == expected


@pytest.mark.parametrize("target", [0, 73])
async def test_mqtt_recheck_rejects_target_removed_during_runtime_apply(
    async_client, db_session, printer_factory, monkeypatch, target
):
    # Synthetic PK0 deliberately admits the first existence check so the late
    # guard itself is exercised. Normally absent zero is refused before apply.
    await printer_factory(id=target)
    from backend.app.api.routes import settings as settings_routes, smart_plugs

    configured = iter([False, True])
    monkeypatch.setattr(smart_plugs.mqtt_relay.smart_plug_service, "is_configured", lambda: next(configured))

    async def apply(db, **kwargs):
        assert not db.in_transaction()  # The route rolled back before runtime I/O.
        await db.execute(delete(Printer).where(Printer.id == target))
        await db.commit()

    runtime = AsyncMock(side_effect=apply)
    monkeypatch.setattr(settings_routes, "_apply_mqtt_runtime", runtime)
    subscribe = MagicMock(return_value=[])
    monkeypatch.setattr(smart_plugs, "subscribe_plug_to_mqtt", subscribe)
    response = await async_client.post(
        "/api/v1/smart-plugs/",
        json={
            "name": "MQTT",
            "plug_type": "mqtt",
            "printer_id": target,
            "mqtt_power_topic": "test/power",
            "enabled": False,
        },
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "Printer not found"
    runtime.assert_awaited_once()
    subscribe.assert_not_called()
    assert await db_session.scalar(select(func.count()).select_from(SmartPlug)) == 0


@pytest.mark.parametrize("field", ["target_printer_id", "target_folder_id"])
@pytest.mark.parametrize("target", [0, 999999])
async def test_vp_create_missing_supplied_target_is_atomic(async_client, db_session, field, target):
    response = await async_client.post(
        "/api/v1/virtual-printers", json={"name": "Target check", "enabled": False, field: target}
    )
    assert response.status_code == 400, response.text
    assert "not found" in response.text and str(target) in response.text
    assert await db_session.scalar(select(func.count()).select_from(VirtualPrinter)) == 0


@pytest.mark.parametrize("selection", ["omitted", "null", "valid"])
async def test_vp_create_optional_and_valid_targets(async_client, db_session, printer_factory, selection):
    printer = await printer_factory()
    folder = LibraryFolder(name="Target folder")
    db_session.add(folder)
    await db_session.commit()
    fields = (
        {}
        if selection == "omitted"
        else {
            "target_printer_id": printer.id if selection == "valid" else None,
            "target_folder_id": folder.id if selection == "valid" else None,
        }
    )
    response = await async_client.post("/api/v1/virtual-printers", json={"name": "Optional targets", **fields})
    assert response.status_code == 200, response.text
    assert response.json()["target_printer_id"] == (printer.id if selection == "valid" else None)
    assert response.json()["target_folder_id"] == (folder.id if selection == "valid" else None)


@pytest.mark.parametrize(
    "fields,reason",
    [
        ({"enabled": True, "target_printer_id": 0}, "Bind IP is required"),
        ({"enabled": True, "bind_ip": "192.0.2.10", "target_printer_id": 0}, "Access code is required"),
        (
            {"enabled": True, "bind_ip": "192.0.2.10", "mode": "proxy", "target_printer_id": 0},
            "Target printer is required",
        ),
        ({"mode": "print_queue", "auto_dispatch": True, "target_printer_id": 0}, "requires a Target Printer"),
        ({"nozzle_diameter": "invalid", "target_printer_id": 0}, "nozzle_diameter"),
    ],
)
async def test_vp_requirements_still_precede_target_lookup(async_client, fields, reason):
    response = await async_client.post("/api/v1/virtual-printers", json=fields)
    assert response.status_code == (422 if "nozzle_diameter" in fields else 400), response.text
    assert reason in response.text and "not found" not in response.text


@pytest.mark.parametrize("field", ["target_printer_id", "target_folder_id"])
async def test_vp_update_existing_lookup_rejects_zero(async_client, db_session, field):
    vp = VirtualPrinter(name="Existing", enabled=False)
    db_session.add(vp)
    await db_session.commit()
    response = await async_client.put(f"/api/v1/virtual-printers/{vp.id}", json={field: 0})
    assert response.status_code == 400, response.text
    await db_session.refresh(vp)
    assert getattr(vp, field) is None


async def test_api_keys_cannot_write_plug_or_vp_even_with_allowed_target(async_client, db_session, printer_factory):
    allowed, hidden = await printer_factory(), await printer_factory(name="Hidden target")
    key_response = await async_client.post(
        "/api/v1/api-keys/", json={"name": "Scoped binding", "can_control_printer": True, "printer_ids": [allowed.id]}
    )
    assert key_response.status_code == 200, key_response.text
    # Replace, rather than supplement, the fixture's default administrator JWT.
    headers = {"Authorization": f"Bearer {key_response.json()['key']}"}
    for target in (hidden.id, allowed.id, 0):
        response = await async_client.post("/api/v1/smart-plugs/", json=plug_body(printer_id=target), headers=headers)
        assert response.status_code == 403, response.text
        assert "administrative operations" in response.text
        assert hidden.name not in response.text
    assert await db_session.scalar(select(func.count()).select_from(SmartPlug)) == 0
    response = await async_client.post(
        "/api/v1/virtual-printers", json={"target_printer_id": hidden.id}, headers=headers
    )
    assert response.status_code in (401, 403), response.text
    assert await db_session.scalar(select(func.count()).select_from(VirtualPrinter)) == 0


@pytest.mark.parametrize("target", [None, 0])
async def test_stored_zero_stays_bound_and_cold_not_manual(db_session, tmp_path, monkeypatch, target):
    from backend.app.services.virtual_printer import manager as manager_module
    from backend.app.services.virtual_printer.manager import VirtualPrinterInstance
    from backend.app.services.virtual_printer.mqtt_server import SimpleMQTTServer

    class IdleTransport:
        def __init__(self, **kwargs):
            self.ready = asyncio.Event()

        async def start(self):
            self.ready.set()
            await asyncio.Event().wait()

    async def mqtt_start(self):
        self.ready.set()
        await asyncio.Event().wait()

    for name in ("VirtualPrinterFTPServer", "BindServer", "VirtualPrinterSSDPServer"):
        monkeypatch.setattr(manager_module, name, IdleTransport)
    monkeypatch.setattr(SimpleMQTTServer, "start", mqtt_start)
    vp = VirtualPrinter(
        name="Historical target", target_printer_id=target, nozzle_diameter="0.6", nozzle_flow="high_flow"
    )
    db_session.add(vp)
    await db_session.commit()
    instance = VirtualPrinterInstance(
        vp_id=vp.id,
        name=vp.name,
        mode="file_manager",
        model="BL-P001",
        access_code="00000000",
        serial_suffix=vp.serial_suffix,
        target_printer_id=vp.target_printer_id,
        nozzle_diameter=vp.nozzle_diameter,
        nozzle_flow=vp.nozzle_flow,
        base_dir=tmp_path,
    )
    monkeypatch.setattr(
        instance, "_resolve_cert_and_advertise", lambda: (tmp_path / "unused.crt", tmp_path / "unused.key", "127.0.0.1")
    )
    try:
        await instance._start_server()
        assert instance._mqtt.has_target is (target is not None)
        instance._mqtt._publish_to_report = AsyncMock()
        await instance._mqtt._send_status_report(MagicMock())
        report = instance._mqtt._publish_to_report.call_args.args[1]["print"]
        expected = ("0.0", "N/A") if target is not None else ("0.6", "HH01")
        assert (report["nozzle_diameter"], report["nozzle_type"]) == expected
    finally:
        for task in instance._tasks:
            task.cancel()
        await asyncio.gather(*instance._tasks, return_exceptions=True)
    await db_session.refresh(vp)
    assert vp.target_printer_id == target
