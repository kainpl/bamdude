"""A real printer-delete route must mark its VP detach while sync is busy."""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.api.routes import printers as routes
from backend.app.models.printer import Printer
from backend.app.models.virtual_printer import VirtualPrinter
from backend.app.services import virtual_printer as service
from backend.app.services.virtual_printer.manager import VirtualPrinterInstance, VirtualPrinterManager
from backend.tests.integration.test_vp_drain_crud import wal_database  # noqa: F401


@pytest.mark.parametrize("delete_archives", [False, True])
async def test_printer_delete_marks_only_detached_vps_before_waiting_for_sync(
    wal_database, tmp_path, monkeypatch, delete_archives
):
    engine, db = wal_database
    from backend.app.services import obico_history

    monkeypatch.setattr(
        obico_history,
        "obico_history",
        obico_history.ObicoHistory(
            root=tmp_path / "obico", session_factory=async_sessionmaker(engine, expire_on_commit=False)
        ),
    )
    doomed = Printer(id=777, name="Deleted", serial_number="DELETE-777", ip_address="127.0.0.1", access_code="12345678")
    kept = Printer(id=888, name="Kept", serial_number="KEEP-888", ip_address="127.0.0.1", access_code="12345678")
    db.add_all([doomed, kept])
    await db.commit()
    vps = [
        VirtualPrinter(
            name=name,
            enabled=True,
            mode="file_manager",
            model="N2S",
            access_code="12345678",
            serial_suffix=f"{i:08}",
            bind_ip="127.0.0.1",
            position=i,
            target_printer_id=target,
        )
        for i, (name, target) in enumerate([("A", None), ("B", 777), ("C", 888)])
    ]
    db.add_all(vps)
    await db.commit()
    starts = []

    async def start(instance):
        starts.append((instance.name, instance.target_printer_id))
        return True

    monkeypatch.setattr(VirtualPrinterInstance, "start_server", start)
    manager = VirtualPrinterManager()
    manager._base_dir = tmp_path
    manager.set_session_factory(async_sessionmaker(engine, expire_on_commit=False))
    monkeypatch.setattr(service, "virtual_printer_manager", manager)
    await manager.sync_from_db()
    a = manager.get_instance(vps[0].id)
    entered, release = asyncio.Event(), asyncio.Event()

    async def upload():
        with a.activities.business():
            entered.set()
            await release.wait()

    work = asyncio.create_task(upload())
    await entered.wait()
    try:
        vps[0].model = "N1"
        await db.commit()
        manager.drain_timeout = 0.05
        await manager.sync_from_db()
        async with asyncio.timeout(5):
            while not a.activities.closing:
                await asyncio.sleep(0.01)
        async with async_sessionmaker(engine, expire_on_commit=False)() as deletion_db:
            response = await routes.delete_printer(777, delete_archives, None, deletion_db)
        assert response == {"status": "deleted", "archives_deleted": delete_archives}
        async with async_sessionmaker(engine, expire_on_commit=False)() as check:
            assert await check.get(Printer, 777) is None
            assert (await check.get(VirtualPrinter, vps[1].id)).target_printer_id is None
        b, c = (manager.get_instance(vp.id) for vp in vps[1:])
        assert b.target_printer_id == 777  # runtime changes only after retirement proof
        assert b.get_status()["runtime_state"] == "stopping"
        assert b.get_status()["runtime_proven"] is False
        assert c.target_printer_id == 888 and c.get_status()["runtime_proven"] is True
        assert not c._config_pending
    finally:
        release.set()
        await work
        manager.drain_timeout = 5
        await manager.sync_from_db()
        after = manager.get_instance(vps[1].id)
        after_status = after.get_status() if after is not None else None
        after_target = after.target_printer_id if after is not None else "missing"
        await manager.stop_all()
    assert after_target is None
    assert after_status["runtime_state"] == "running" and after_status["runtime_proven"] is True
    assert ("B", None) in starts and starts.count(("B", 777)) == 1
    assert starts.count(("C", 888)) == 1
