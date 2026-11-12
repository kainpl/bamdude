"""Persisted CRUD and deferred replacement while the predecessor is retiring."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core.database import Base, import_all_models
from backend.app.models.virtual_printer import VirtualPrinter
from backend.app.services.farm_startup_guard import farm_startup_guard
from backend.app.services.virtual_printer import virtual_printer_manager
from backend.app.services.virtual_printer.manager import VirtualPrinterInstance, VirtualPrinterManager


@pytest.fixture
async def wal_database(tmp_path):
    import_all_models()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'vp-wal.sqlite'}")
    async with engine.begin() as connection:
        await connection.execute(text("PRAGMA journal_mode=WAL"))
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            yield engine, session
    finally:
        await engine.dispose()


async def test_repeated_edit_waits_predecessor_then_starts_only_latest_config(wal_database, tmp_path, monkeypatch):
    test_engine, db_session = wal_database
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
    async with asyncio.timeout(5):
        while not old.activities.closing:
            await asyncio.sleep(0.01)
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


async def test_failed_predecessor_does_not_abort_the_next_vp(wal_database, tmp_path, monkeypatch):
    from types import SimpleNamespace

    engine, db = wal_database
    rows = [
        VirtualPrinter(
            name=name,
            enabled=True,
            mode="file_manager",
            model="N2S",
            access_code="12345678",
            serial_suffix=f"{i:08}",
            bind_ip="127.0.0.1",
            position=i,
        )
        for i, name in enumerate(["A", "B"])
    ]
    db.add_all(rows)
    await db.commit()
    starts = []

    async def start(inst):
        starts.append((inst.name, inst.model))
        return True

    monkeypatch.setattr(VirtualPrinterInstance, "start_server", start)
    manager = VirtualPrinterManager()
    manager._base_dir = tmp_path
    manager.set_session_factory(async_sessionmaker(engine, expire_on_commit=False))
    await manager.sync_from_db()
    old = manager.get_instance(rows[0].id)
    old._mqtt_bridge = SimpleNamespace(stop=AsyncMock(side_effect=OSError("cleanup could not be confirmed")))
    for row in rows:
        row.model = "N1"
    await db.commit()
    await manager.sync_from_db()
    assert ("B", "N1") in starts and ("A", "N1") not in starts
    assert manager.get_instance(old.id) is old and old.id in manager._unproven
    assert not manager._retiring
    await manager.stop_all()


async def test_unrelated_vp_is_not_marked_config_pending(wal_database, tmp_path, monkeypatch):
    engine, db = wal_database
    rows = [
        VirtualPrinter(
            name=name,
            enabled=True,
            mode="file_manager",
            model="N2S",
            access_code="12345678",
            serial_suffix=f"{i:08}",
            bind_ip="127.0.0.1",
            position=i,
        )
        for i, name in enumerate(["A", "B"])
    ]
    db.add_all(rows)
    await db.commit()

    async def start(inst):
        return True

    monkeypatch.setattr(VirtualPrinterInstance, "start_server", start)
    manager = VirtualPrinterManager()
    manager._base_dir = tmp_path
    manager.set_session_factory(async_sessionmaker(engine, expire_on_commit=False))
    await manager.sync_from_db()
    a, b = (manager.get_instance(row.id) for row in rows)
    entered, release = asyncio.Event(), asyncio.Event()

    async def work():
        with a.activities.business():
            entered.set()
            await release.wait()

    work_task = asyncio.create_task(work())
    await entered.wait()
    rows[0].model = "N1"
    await db.commit()
    manager.drain_timeout = 0.01
    await manager.sync_from_db()
    async with asyncio.timeout(5):
        while not a.activities.closing:
            await asyncio.sleep(0.01)
    assert a.get_status()["runtime_state"] == "stopping"
    assert b.get_status()["runtime_state"] == "running" and not b._config_pending
    release.set()
    await work_task
    manager.drain_timeout = 5
    await manager.sync_from_db()
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


async def test_reused_id_waits_for_deleted_owners_files_without_another_sync(wal_database, tmp_path, monkeypatch):
    engine, db = wal_database
    row = VirtualPrinter(
        name="Old",
        enabled=True,
        mode="file_manager",
        model="N2S",
        access_code="12345678",
        serial_suffix="12345678",
        bind_ip="127.0.0.1",
    )
    db.add(row)
    await db.commit()
    vp_id = row.id
    starts = []

    async def start(inst):
        starts.append(inst.model)
        if inst.model == "N7":
            path = tmp_path / "uploads" / str(inst.id)
            path.mkdir(parents=True, exist_ok=True)
            (path / "new.3mf").write_bytes(b"new owner")
        return True

    monkeypatch.setattr(VirtualPrinterInstance, "start_server", start)
    manager = VirtualPrinterManager()
    manager._base_dir = tmp_path
    manager.set_session_factory(async_sessionmaker(engine, expire_on_commit=False))
    await manager.sync_from_db()
    old = manager.get_instance(vp_id)
    upload = tmp_path / "uploads" / str(vp_id)
    (upload / "old.3mf").write_bytes(b"old owner")
    entered, release = asyncio.Event(), asyncio.Event()

    async def work():
        with old.activities.business():
            entered.set()
            await release.wait()

    business = asyncio.create_task(work())
    await entered.wait()
    reservation = manager.begin_delete(vp_id)
    manager.drain_timeout = 0.01
    assert not await manager.remove_instance(vp_id)
    await db.delete(row)
    await db.commit()
    manager.finish_delete(vp_id, reservation, committed=True)
    db.add(
        VirtualPrinter(
            id=vp_id,
            name="New",
            enabled=True,
            mode="file_manager",
            model="N7",
            access_code="12345678",
            serial_suffix="87654321",
            bind_ip="127.0.0.1",
        )
    )
    await db.commit()
    manager.drain_timeout = 5
    sync = asyncio.create_task(manager.sync_from_db())
    await asyncio.sleep(0.05)
    assert starts == ["N2S"] and (upload / "old.3mf").exists()
    release.set()
    await business
    await sync
    assert starts == ["N2S", "N7"] and not (upload / "old.3mf").exists()
    manager.finish_delete(vp_id, reservation, committed=True)
    assert (upload / "new.3mf").read_bytes() == b"new owner"
    await manager.stop_all()


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
