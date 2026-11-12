"""Persisted CRUD and deferred replacement while the predecessor is retiring."""

import asyncio
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.virtual_printer import VirtualPrinter
from backend.app.services.farm_startup_guard import farm_startup_guard
from backend.app.services.virtual_printer import virtual_printer_manager
from backend.app.services.virtual_printer.manager import VirtualPrinterInstance, VirtualPrinterManager


async def test_repeated_edit_waits_predecessor_then_starts_only_latest_config(
    test_engine, db_session, tmp_path, monkeypatch
):
    row = VirtualPrinter(
        name="Owned",
        enabled=True,
        mode="file_manager",
        model="N2S",
        access_code="12345678",
        serial_suffix="12345678",
        bind_ip="127.0.0.1",
    )
    db_session.add(row)
    await db_session.commit()
    starts = []

    async def start(instance):
        starts.append(instance.model)
        return True

    monkeypatch.setattr(VirtualPrinterInstance, "start_server", start)
    manager = VirtualPrinterManager()
    manager._base_dir = tmp_path
    manager.set_session_factory(async_sessionmaker(test_engine, expire_on_commit=False))
    await manager.sync_from_db()
    old = manager.get_instance(row.id)
    assert old is not None
    entered, release = asyncio.Event(), asyncio.Event()

    async def business():
        with old.activities.business():
            entered.set()
            await release.wait()

    work = asyncio.create_task(business())
    await entered.wait()
    row.model = "N1"
    await db_session.commit()
    manager.drain_timeout = 0.02
    await manager.sync_from_db()
    assert manager.get_instance(row.id) is old
    assert starts == ["N2S"]
    assert old.get_status()["runtime_state"] == "stopping"
    row.model = "N7"
    await db_session.commit()
    await manager.sync_from_db()
    assert starts == ["N2S"]  # no replacement beside the still-writing predecessor
    release.set()
    await work
    manager.drain_timeout = 5
    await manager.sync_from_db()
    assert starts == ["N2S", "N7"]
    assert manager.get_instance(row.id) is not old
    await manager.stop_all()


async def test_latched_delete_preserves_upload_files_and_does_no_runtime_cleanup(
    committing_client, tmp_path, monkeypatch
):
    response = await committing_client.post("/api/v1/virtual-printers", json={"name": "Dormant", "enabled": False})
    assert response.status_code == 200
    vp_id = response.json()["id"]
    monkeypatch.setattr(virtual_printer_manager, "_base_dir", tmp_path)
    upload = tmp_path / "uploads" / str(vp_id)
    upload.mkdir(parents=True)
    retained = upload / "accepted.3mf"
    retained.write_bytes(b"owned")
    monkeypatch.setattr(farm_startup_guard, "restart_required", True)
    response = await committing_client.delete(f"/api/v1/virtual-printers/{vp_id}")
    assert response.status_code == 200
    assert retained.read_bytes() == b"owned"
    assert (await committing_client.get(f"/api/v1/virtual-printers/{vp_id}")).status_code == 404


async def test_latched_ca_request_never_constructs_a_certificate_writer(committing_client, tmp_path, monkeypatch):
    from unittest.mock import Mock

    from backend.app.services.virtual_printer import manager as implementation

    monkeypatch.setattr(virtual_printer_manager, "_base_dir", tmp_path / "not-created")
    writer = Mock(side_effect=AssertionError("Certificate writer ran after restore latch"))
    monkeypatch.setattr(implementation, "CertificateService", writer)
    monkeypatch.setattr(farm_startup_guard, "restart_required", True)
    response = await committing_client.get("/api/v1/virtual-printers/ca-certificate")
    assert response.status_code == 409
    assert "paused for restore" in response.json()["detail"]
    writer.assert_not_called()
    assert not (tmp_path / "not-created").exists()
