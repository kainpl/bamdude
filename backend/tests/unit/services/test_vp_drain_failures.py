"""Finished failures are not work; genuinely unproven cleanup remains explicit."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.services.virtual_printer.lifecycle import VP_RESTART_MESSAGE, VPDrainRefused
from backend.app.services.virtual_printer.manager import VirtualPrinterManager
from backend.app.services.virtual_printer.tcp_proxy import SlicerProxyManager, TLSProxy
from backend.tests.unit.services.test_vp_owned_drain import instance


async def test_failed_cleanup_finishes_other_services_and_requires_restart(tmp_path):
    inst = instance(tmp_path)
    bridge = SimpleNamespace(stop=AsyncMock(side_effect=OSError("bridge cleanup failed")))
    ftp = SimpleNamespace(close_admission=lambda: None, stop=AsyncMock())
    ssdp = SimpleNamespace(stop=AsyncMock())
    inst._mqtt_bridge, inst._ftp, inst._ssdp = bridge, ftp, ssdp
    manager = VirtualPrinterManager()
    manager._instances[inst.id] = inst
    assert not await manager.remove_instance(inst.id)
    ftp.stop.assert_awaited_once()
    ssdp.stop.assert_awaited_once()
    assert inst._mqtt_bridge is bridge
    assert inst.id not in manager._retiring
    assert manager.get_instance(inst.id) is inst and manager.has_owned_instance(inst.id)
    for _ in range(2):
        with pytest.raises(VPDrainRefused, match=VP_RESTART_MESSAGE):
            await manager.drain_for_restore()
    assert manager._drain_task.done()


@pytest.mark.parametrize("cancelled", [False, True])
async def test_exited_activity_can_join_even_when_it_failed_during_close(tmp_path, cancelled):
    inst = instance(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def work():
        with inst.activities.business():
            entered.set()
            await release.wait()
            raise OSError("callback failed after publication")

    business = asyncio.create_task(work())
    await entered.wait()
    stop = asyncio.create_task(inst.stop_server())
    await asyncio.sleep(0)
    assert not stop.done()
    if cancelled:
        business.cancel()
    else:
        release.set()
    await asyncio.gather(business, return_exceptions=True)
    await stop
    assert inst.get_status()["runtime_proven"]


async def test_proxy_camera_lost_ack_refuses_after_idle_children_are_closed(tmp_path, monkeypatch):
    from backend.app.services import camera_runtime
    from backend.app.utils import printer_configs

    proxy = SlicerProxyManager("127.0.0.1", target_model="A1", cert_path=tmp_path / "c", key_path=tmp_path / "k")
    runtime = object.__new__(camera_runtime.WorkerCameraRuntime)
    entered, release = asyncio.Event(), asyncio.Event()

    async def acquire(**kwargs):
        entered.set()
        await release.wait()
        raise OSError("camera start acknowledgement lost")

    runtime.supervisor = SimpleNamespace(start_raw_proxy=acquire)
    monkeypatch.setattr(camera_runtime, "get_camera_runtime", lambda: runtime)
    monkeypatch.setattr(printer_configs, "load_printer_config", lambda *a: {})
    camera = asyncio.create_task(proxy.reconcile_camera_proxy())
    await entered.wait()
    child = SimpleNamespace(close_admission=lambda: None, stop=AsyncMock(), connections=SimpleNamespace(cuts=0))
    proxy._mqtt_proxy = child
    stop = asyncio.create_task(proxy.stop())
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(camera, return_exceptions=True)
    with pytest.raises(VPDrainRefused, match=VP_RESTART_MESSAGE):
        await stop
    child.stop.assert_awaited_once()


async def test_outgoing_proxy_tls_keeps_the_existing_connect_budget(tmp_path, monkeypatch):
    proxy = TLSProxy("mqtt", 0, "127.0.0.1", 1, tmp_path / "c", tmp_path / "k")
    seen = {}

    async def connect(*args, **kwargs):
        seen.update(kwargs)
        raise OSError("offline private target")

    monkeypatch.setattr(asyncio, "open_connection", connect)
    writer = SimpleNamespace(
        get_extra_info=lambda _: ("127.0.0.1", 1),
        close=lambda: None,
        wait_closed=AsyncMock(),
    )
    await proxy._handle_client(asyncio.StreamReader(), writer)
    assert "ssl" in seen and "ssl_handshake_timeout" not in seen


async def test_delete_cleans_only_after_commit_and_proof_before_id_reuse(tmp_path, monkeypatch):
    inst = instance(tmp_path)
    manager = VirtualPrinterManager()
    manager._base_dir = tmp_path
    manager._instances[inst.id] = inst
    upload = tmp_path / "uploads" / str(inst.id)
    upload.mkdir(parents=True, exist_ok=True)
    (upload / "accepted.3mf").write_bytes(b"owned")
    entered, release = asyncio.Event(), asyncio.Event()

    async def work():
        with inst.activities.business():
            entered.set()
            await release.wait()

    business = asyncio.create_task(work())
    await entered.wait()
    reservation = manager.begin_delete(inst.id)
    manager.drain_timeout = 0.01
    assert not await manager.remove_instance(inst.id)
    manager.finish_delete(inst.id, reservation, committed=True)
    assert upload.exists() and inst.id in manager._deletions
    root = manager._retiring[inst.id][1]
    release.set()
    await business
    await root
    assert not upload.exists() and inst.id not in manager._deletions
    # A new owner can now create its own directory; stale completion cannot erase it.
    upload.mkdir()
    fresh = upload / "new.3mf"
    fresh.write_bytes(b"new owner")
    manager.finish_delete(inst.id, reservation, committed=True)
    assert fresh.read_bytes() == b"new owner"


async def test_delete_does_not_clean_before_db_commit_or_after_db_failure(tmp_path):
    manager = VirtualPrinterManager()
    manager._base_dir = tmp_path
    upload = tmp_path / "uploads" / "7"
    upload.mkdir(parents=True)
    reservation = manager.begin_delete(7)
    assert await manager.remove_instance(7)
    assert upload.exists()
    manager.finish_delete(7, reservation, committed=False)
    assert upload.exists() and 7 not in manager._deletions
