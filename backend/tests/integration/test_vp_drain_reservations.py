"""Regression for deletion-reservation failure wakeups and saved-config status."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core.database import Base, import_all_models
from backend.app.models.virtual_printer import VirtualPrinter
from backend.app.services.virtual_printer.lifecycle import VPDrainRefused
from backend.app.services.virtual_printer.manager import VirtualPrinterInstance, VirtualPrinterManager


def _record(tag, data):
    line = f"{tag} {json.dumps(data, default=str, sort_keys=True)}"
    print(line)


@pytest.fixture
async def wal_db(tmp_path):
    import_all_models()
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'opus-wal.sqlite').as_posix()}")
    async with engine.begin() as connection:
        await connection.execute(text("PRAGMA journal_mode=WAL"))
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


def _row(name, pos, model="N2S", vp_id=None):
    kwargs = {
        "name": name,
        "enabled": True,
        "mode": "file_manager",
        "model": model,
        "access_code": "12345678",
        "serial_suffix": f"{pos + 1:08}",
        "bind_ip": "127.0.0.1",
        "position": pos,
    }
    if vp_id is not None:
        kwargs["id"] = vp_id
    return VirtualPrinter(**kwargs)


async def _set_model(engine, vp_id, model):
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        vp = await s.get(VirtualPrinter, vp_id)
        vp.model = model
        await s.commit()


async def _drain(manager, tries=2):
    out = []
    for _ in range(tries):
        try:
            await manager.drain_for_restore()
            out.append("proven")
        except VPDrainRefused as exc:
            out.append(exc.reason)
    return out


def _manager(engine, base, starts, monkeypatch):
    async def start(instance):
        starts.append((instance.name, instance.model))
        return True

    monkeypatch.setattr(VirtualPrinterInstance, "start_server", start)
    manager = VirtualPrinterManager()
    manager._base_dir = base
    manager.set_session_factory(async_sessionmaker(engine, expire_on_commit=False))
    return manager


def _hold(instance):
    entered, release = asyncio.Event(), asyncio.Event()

    async def work():
        with instance.activities.business():
            entered.set()
            await release.wait()

    return asyncio.create_task(work()), entered, release


async def test_d1_reused_id_and_failed_deleted_owner(wal_db, tmp_path, monkeypatch):
    async def run(fail):
        async with async_sessionmaker(wal_db, expire_on_commit=False)() as s:
            await s.execute(text("DELETE FROM virtual_printers"))
            other, old = _row("C", 0), _row("Old", 1)
            s.add_all([other, old])
            await s.commit()
            c_id, vp_id = other.id, old.id
        starts = []
        base = tmp_path / ("fail" if fail else "ok")
        manager = _manager(wal_db, base, starts, monkeypatch)
        await manager.sync_from_db()
        inst = manager.get_instance(vp_id)
        if fail:  # the deleted owner's teardown cannot be confirmed (same injection as the author's test)
            inst._mqtt_bridge = SimpleNamespace(stop=AsyncMock(side_effect=OSError("cleanup could not be confirmed")))
        upload = base / "uploads" / str(vp_id)
        upload.mkdir(parents=True, exist_ok=True)
        (upload / "old.3mf").write_bytes(b"old owner")
        business, entered, release = _hold(inst)
        await entered.wait()
        # The delete route, step by step.
        reservation = manager.begin_delete(vp_id)
        manager.drain_timeout = 0.05
        removed = await manager.remove_instance(vp_id)
        async with async_sessionmaker(wal_db, expire_on_commit=False)() as s:
            await s.execute(text("DELETE FROM virtual_printers WHERE id = :i"), {"i": vp_id})
            await s.commit()
        manager.finish_delete(vp_id, reservation, committed=True)
        await manager.sync_from_db()  # the route's own resync
        # A new VP gets the same id (SQLite max(id)+1 after deleting the newest row).
        async with async_sessionmaker(wal_db, expire_on_commit=False)() as s:
            new = _row("New", 2, model="N7")
            s.add(new)
            await s.commit()
            reused = new.id == vp_id
        manager.drain_timeout = 0.3
        await manager.sync_from_db()  # the create route's sync: waits on the reservation
        release.set()
        await business
        await asyncio.sleep(0.5)  # the deleted owner's retirement completes (or fails)
        state = {
            "remove_instance": removed,
            "id_reused": reused,
            "deleted_owner_unproven": vp_id in manager._unproven,
            "reservation_done": reservation["done"].is_set(),
            "reservation_retained": manager._deletions.get(vp_id) is reservation,
            "sync_task_done": manager._sync_task.done() if manager._sync_task else None,
            "new_vp_started": ["New", "N7"] in [list(x) for x in starts],
        }
        # An ordinary edit of the unrelated VP C after that.
        await _set_model(wal_db, c_id, "N1")
        await manager.sync_from_db()
        await asyncio.sleep(0.3)
        state["c_edit_applied"] = ["C", "N1"] in [list(x) for x in starts]
        state["drain"] = await _drain(manager)
        state["drain_root_done"] = manager._drain_task.done()
        state["old_upload_left"] = (upload / "old.3mf").exists()
        # Defensive cleanup after assertions have captured the coordinator state.
        reservation["done"].set()
        manager.drain_timeout = 5
        if manager._drain_task is not None:
            await asyncio.wait({manager._drain_task}, timeout=5)
        return state

    control, failed = await run(False), await run(True)
    _record("D1", {"control_retirement_ok": control, "deleted_owner_retirement_failed": failed})
    assert control["reservation_done"] and control["new_vp_started"] and control["c_edit_applied"]
    assert control["drain"] == ["proven", "proven"]
    # A terminal failed retirement wakes the waiter but keeps the ID unproven.
    assert failed["deleted_owner_unproven"] and failed["reservation_done"]
    assert failed["reservation_retained"]
    assert failed["sync_task_done"] is True
    assert failed["c_edit_applied"]
    assert not failed["new_vp_started"] and failed["old_upload_left"]
    assert failed["drain"] == ["cleanup_failed", "cleanup_failed"] and failed["drain_root_done"]


async def test_d2_stale_read_waiting_on_a_failed_deleted_owner(wal_db, tmp_path, monkeypatch):
    async def run(fail):
        async with async_sessionmaker(wal_db, expire_on_commit=False)() as s:
            await s.execute(text("DELETE FROM virtual_printers"))
            c, a = _row("C", 0), _row("A", 1)
            s.add_all([c, a])
            await s.commit()
            c_id, a_id = c.id, a.id
        starts = []
        manager = _manager(wal_db, tmp_path / ("fail" if fail else "ok"), starts, monkeypatch)
        await manager.sync_from_db()
        inst_c, inst_a = manager.get_instance(c_id), manager.get_instance(a_id)
        if fail:
            inst_a._mqtt_bridge = SimpleNamespace(stop=AsyncMock(side_effect=OSError("cleanup could not be confirmed")))
        c_work, c_in, c_release = _hold(inst_c)
        a_work, a_in, a_release = _hold(inst_a)
        await c_in.wait()
        await a_in.wait()
        # An edit of C: the runner reads [C, A] and waits for C's retirement (an upload in flight on C).
        await _set_model(wal_db, c_id, "N1")
        manager.drain_timeout = 0.05
        await manager.sync_from_db()
        # The operator deletes A while the runner is still working.
        reservation = manager.begin_delete(a_id)
        manager.drain_timeout = 1.0
        remove = asyncio.create_task(manager.remove_instance(a_id))  # the route waits here (30 s in production)
        await asyncio.sleep(0.05)
        c_release.set()  # C's upload ends: the runner continues to A, still retiring, and waits on the reservation
        await c_work
        removed = await remove
        async with async_sessionmaker(wal_db, expire_on_commit=False)() as s:
            await s.execute(text("DELETE FROM virtual_printers WHERE id = :i"), {"i": a_id})
            await s.commit()
        manager.finish_delete(a_id, reservation, committed=True)
        manager.drain_timeout = 0.3
        await manager.sync_from_db()  # the route's resync
        a_release.set()
        await a_work
        await asyncio.sleep(0.5)
        state = {
            "remove_instance": removed,
            "a_unproven": a_id in manager._unproven,
            "reservation_done": reservation["done"].is_set(),
            "reservation_retained": manager._deletions.get(a_id) is reservation,
            "sync_task_done": manager._sync_task.done() if manager._sync_task else None,
            "c_restarted": ["C", "N1"] in [list(x) for x in starts],
        }
        await _set_model(wal_db, c_id, "N7")
        await manager.sync_from_db()
        await asyncio.sleep(0.3)
        state["c_second_edit_applied"] = ["C", "N7"] in [list(x) for x in starts]
        state["drain"] = await _drain(manager)
        state["drain_root_done"] = manager._drain_task.done()
        reservation["done"].set()
        if manager._drain_task is not None:
            await asyncio.wait({manager._drain_task}, timeout=5)
        return state

    control, failed = await run(False), await run(True)
    _record("D2", {"control_retirement_ok": control, "deleted_owner_retirement_failed": failed})
    assert control["reservation_done"] and control["c_second_edit_applied"]
    assert control["drain"] == ["proven", "proven"]
    assert failed["a_unproven"] and failed["reservation_done"] and failed["sync_task_done"] is True
    assert failed["reservation_retained"]
    assert failed["c_second_edit_applied"]
    assert failed["drain"] == ["cleanup_failed", "cleanup_failed"] and failed["drain_root_done"]


async def test_s1_edit_of_b_while_runner_is_held_by_a(wal_db, tmp_path, monkeypatch):
    async with async_sessionmaker(wal_db, expire_on_commit=False)() as s:
        a, b = _row("A", 0), _row("B", 1)
        s.add_all([a, b])
        await s.commit()
        a_id, b_id = a.id, b.id
    starts = []
    manager = _manager(wal_db, tmp_path, starts, monkeypatch)
    await manager.sync_from_db()
    inst_a = manager.get_instance(a_id)
    work, entered, release = _hold(inst_a)
    await entered.wait()
    await _set_model(wal_db, a_id, "N1")
    manager.drain_timeout = 0.05
    await manager.sync_from_db()  # runner now waits for A's retirement
    from backend.app.api.routes import virtual_printers as routes
    from backend.app.services import virtual_printer as service

    monkeypatch.setattr(service, "virtual_printer_manager", manager)
    async with async_sessionmaker(wal_db, expire_on_commit=False)() as db:
        response = await routes.update_virtual_printer(b_id, routes.VirtualPrinterUpdate(model="N1"), db, None)
    status_b = manager.get_instance(b_id).get_status()
    record = {
        "b_saved_model": "N1",
        "b_running_model": manager.get_instance(b_id).model,
        "b_runtime_state": status_b["runtime_state"],
        "b_runtime_proven": status_b["runtime_proven"],
        "b_running_flag": status_b["running"],
    }
    release.set()
    await work
    manager.drain_timeout = 5
    await manager.sync_from_db()
    record["after_release_starts"] = starts
    _record("S1", record)
    await manager.stop_all()
    assert record["b_running_model"] == "N2S"
    assert response["model"] == "N1"
    assert response["status"]["runtime_state"] == "stopping"
    assert response["status"]["runtime_proven"] is False
    assert record["b_runtime_state"] == "stopping" and record["b_runtime_proven"] is False
    assert ("B", "N1") in starts
