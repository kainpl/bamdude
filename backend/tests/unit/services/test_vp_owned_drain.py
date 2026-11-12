"""F admission/retirement proofs, real loopback sockets, no physical printer."""

import asyncio
import json
import os
import socket
import ssl
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.services.farm_startup_guard import FarmStartupGuard, farm_startup_guard
from backend.app.services.virtual_printer.certificate import CertificateService
from backend.app.services.virtual_printer.ftp_server import VirtualPrinterFTPServer
from backend.app.services.virtual_printer.lifecycle import Activities, VPDrainRefused, close_writer
from backend.app.services.virtual_printer.manager import VirtualPrinterInstance, VirtualPrinterManager
from backend.app.services.virtual_printer.mqtt_server import SimpleMQTTServer
from backend.app.services.virtual_printer.tcp_proxy import TCPProxy, TLSProxy


@pytest.fixture(autouse=True)
def clean_latch(monkeypatch):
    monkeypatch.setattr(farm_startup_guard, "restart_required", False)


def instance(tmp_path):
    return VirtualPrinterInstance(
        vp_id=7,
        name="Owned",
        mode="print_queue",
        model="N2S",
        access_code="12345678",
        serial_suffix="12345678",
        base_dir=tmp_path,
    )


def certificates(tmp_path):
    return CertificateService(
        cert_dir=tmp_path / "cert", serial="01P00A000000000", shared_ca_dir=tmp_path / "ca"
    ).generate_certificates()


def client_context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def connect_packet():
    def string(b):
        return len(b).to_bytes(2, "big") + b

    body = string(b"MQTT") + bytes([4, 0xC2, 0, 60]) + string(b"slicer") + string(b"bblp") + string(b"12345678")
    return bytes([0x10, len(body)]) + body


async def eventual(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


def test_inactive_restore_latches_but_generic_hold_does_not():
    guard = FarmStartupGuard()
    guard.hold("operator")
    assert not guard.restart_required
    guard.hold("restore")
    assert guard.restart_required and guard.status()["state"] == "restart_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["ftp", "mqtt", "pre_connect"])
@pytest.mark.parametrize("entry", ["instance", "remove", "shutdown", "restore"])
async def test_real_idle_clients_stop_without_peer_eof_or_lock_join(tmp_path, protocol, entry):
    cert, key = certificates(tmp_path)
    inst = instance(tmp_path)
    if protocol == "ftp":
        service = VirtualPrinterFTPServer(
            tmp_path / "uploads", "12345678", cert, key, port=0, bind_address="127.0.0.1", activities=inst.activities
        )
        inst._ftp = service
    else:
        service = SimpleMQTTServer(
            "01P00A000000000", "12345678", cert, key, port=0, bind_address="127.0.0.1", activities=inst.activities
        )
        inst._mqtt = service
    inst._tasks = [asyncio.create_task(service.start())]
    await asyncio.wait_for(service.ready.wait(), 5)
    port = service._server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=client_context())
    if protocol == "ftp":
        assert (await asyncio.wait_for(reader.readline(), 3)).startswith(b"220")
    elif protocol == "mqtt":
        writer.write(connect_packet())
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(4), 3) == b"\x20\x02\x00\x00"
    await eventual(lambda: len(service.connections.handlers) == 1)
    manager = VirtualPrinterManager()
    manager._instances[inst.id] = inst
    manager.drain_timeout = 5
    stop = {
        "instance": inst.stop_server,
        "remove": lambda: manager.remove_instance(inst.id),
        "shutdown": manager.stop_all,
        "restore": manager.drain_for_restore,
    }[entry]
    try:
        stopping = asyncio.create_task(stop())
        # Another caller can enter the manager lock while network teardown runs.
        async with asyncio.timeout(0.5):
            async with manager._sync_lock:
                pass
        await asyncio.wait_for(asyncio.shield(stopping), 5)
        assert not inst._tasks and not service.connections.handlers
        assert service._server is None
    finally:
        await close_writer(writer)
        await inst.stop_server()


@pytest.mark.asyncio
async def test_half_open_tls_peer_is_aborted_within_short_grace(tmp_path):
    """ssl.SSLSocket doesn't read close_notify while the notebook is asleep."""
    cert, key = certificates(tmp_path)
    server = SimpleMQTTServer("01P00A000000000", "12345678", cert, key, port=0, bind_address="127.0.0.1")
    service = asyncio.create_task(server.start())
    await asyncio.wait_for(server.ready.wait(), 5)
    port = server._server.sockets[0].getsockname()[1]

    def open_sleeping_peer():
        peer = client_context().wrap_socket(socket.create_connection(("127.0.0.1", port)), server_hostname="localhost")
        return peer  # no asyncio reader; it never consumes/sends close_notify

    peer = await asyncio.to_thread(open_sleeping_peer)
    await eventual(lambda: bool(server.connections.handlers))
    start = time.monotonic()
    try:
        await asyncio.wait_for(server.stop(), 4)
        assert time.monotonic() - start < 4
        assert not server.connections.handlers
    finally:
        peer.close()
        service.cancel()
        await asyncio.gather(service, return_exceptions=True)


@pytest.mark.asyncio
async def test_mqtt_lease_precedes_response_and_timeout_cancel_retains_root(tmp_path, monkeypatch):
    inst = instance(tmp_path)
    mqtt = SimpleMQTTServer("SERIAL", "12345678", tmp_path / "c", tmp_path / "k", activities=inst.activities)
    inst._mqtt = mqtt
    entered, release = asyncio.Event(), asyncio.Event()

    async def response(*args, **kwargs):
        entered.set()
        await release.wait()

    callback = AsyncMock()
    mqtt.on_print_command = callback
    monkeypatch.setattr(mqtt, "_send_print_response", response)
    topic = b"device/SERIAL/request"
    body = json.dumps({"print": {"command": "project_file", "file": "file.3mf"}}).encode()
    payload = len(topic).to_bytes(2, "big") + topic + body
    writer = MagicMock(wait_closed=AsyncMock(), drain=AsyncMock())
    publish = asyncio.create_task(mqtt._handle_publish(0x30, payload, writer, "client"))
    await entered.wait()
    manager = VirtualPrinterManager()
    manager._instances[inst.id] = inst
    manager.drain_timeout = 0.01
    with pytest.raises(VPDrainRefused):
        await manager.drain_for_restore()
    root = manager._drain_task
    assert not root.done() and not publish.done()
    retry = asyncio.create_task(manager.drain_for_restore())
    await asyncio.sleep(0)
    retry.cancel()
    with pytest.raises(asyncio.CancelledError):
        await retry
    assert manager._drain_task is root and not root.cancelled()
    release.set()
    await publish
    manager.drain_timeout = 5
    await manager.drain_for_restore()
    callback.assert_awaited_once()


@pytest.mark.asyncio
async def test_pending_retirement_does_not_reenter_lock_or_allow_latch_crud(tmp_path, monkeypatch):
    inst = instance(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def business():
        with inst.activities.business():
            entered.set()
            await release.wait()

    work = asyncio.create_task(business())
    await entered.wait()
    manager = VirtualPrinterManager()
    manager.drain_timeout = 0.01
    manager._instances[inst.id] = inst
    assert not await manager.remove_instance(inst.id)
    assert manager.get_instance(inst.id).get_status()["runtime_state"] == "stopping"
    async with asyncio.timeout(0.1):
        async with manager._sync_lock:
            assert manager._retire_locked(inst.id) is manager._retiring[inst.id][1]
    monkeypatch.setattr(farm_startup_guard, "restart_required", True)
    assert not await manager.remove_instance(inst.id)
    await manager.sync_from_db()
    await manager.reconcile_camera_proxies()
    assert not work.done()
    release.set()
    await work
    manager.drain_timeout = 5
    await manager.drain_for_restore()


@pytest.mark.asyncio
@pytest.mark.parametrize("tls", [False, True])
async def test_real_proxy_persistent_stream_cut_counted_and_joined(tmp_path, tls):
    upstream_writers = []

    async def upstream(reader, writer):
        upstream_writers.append(writer)
        try:
            await reader.read()
        finally:
            await close_writer(writer)

    cert, key = certificates(tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    remote = await asyncio.start_server(
        upstream, "127.0.0.1", 0, ssl=context if tls else None, **({"ssl_shutdown_timeout": 1} if tls else {})
    )
    kwargs = {
        "name": "MQTT",
        "listen_port": 0,
        "target_host": "127.0.0.1",
        "target_port": remote.sockets[0].getsockname()[1],
        "bind_address": "127.0.0.1",
    }
    if tls:
        proxy = TLSProxy(**kwargs, server_cert_path=cert, server_key_path=key)
    else:
        proxy = TCPProxy(**kwargs)
    service = asyncio.create_task(proxy.start())
    await asyncio.wait_for(proxy.ready.wait(), 5)
    reader, writer = await asyncio.open_connection(
        "127.0.0.1", proxy._server.sockets[0].getsockname()[1], ssl=client_context() if tls else None
    )
    try:
        await eventual(lambda: bool(proxy._active_connections))
        await asyncio.wait_for(proxy.stop(), 5)
        assert proxy.connections.cuts == 1
        assert not proxy.connections.handlers and not proxy._active_connections
        await proxy.stop()
        assert proxy.connections.cuts == 1
    finally:
        await close_writer(writer)
        service.cancel()
        await asyncio.gather(service, return_exceptions=True)
        remote.close()
        await remote.wait_closed()


@pytest.mark.asyncio
async def test_scheduled_busy_keeps_due_slot_and_last_result(tmp_path, monkeypatch):
    from backend.app.api.routes import settings as routes
    from backend.app.services.backup_files import exclusive_operation
    from backend.app.services.local_backup import LocalBackupService

    service = LocalBackupService()
    settings = {"enabled": True, "schedule": "daily", "time": "03:00", "path": str(tmp_path), "retention": 3}
    service._next_run = datetime.now(timezone.utc) - timedelta(seconds=1)
    due = service._next_run
    service._last_status = "success"
    service._last_backup_at = "last-success"
    monkeypatch.setattr(service, "_load_settings", AsyncMock(return_value=settings))
    async with exclusive_operation():
        await service._check_scheduled_backup()
    assert service._next_run == due and service._last_status == "success"
    assert service._last_backup_at == "last-success"
    fake = tmp_path / "backup.zip"
    fake.write_bytes(b"ok")
    monkeypatch.setattr(routes, "create_backup_zip", AsyncMock(return_value=(fake, fake.name)))
    await service._check_scheduled_backup()
    assert service._next_run > due and service._last_status == "success"


@pytest.mark.asyncio
async def test_root_published_before_lock_wait_and_one_deadline(tmp_path):
    manager = VirtualPrinterManager()
    manager.drain_timeout = 0.02
    await manager._sync_lock.acquire()
    try:
        with pytest.raises(VPDrainRefused):
            await manager.drain_for_restore()
        root = manager._drain_task
        assert root is not None and not root.done()
        with pytest.raises(VPDrainRefused):
            await manager.drain_for_restore()
        assert manager._drain_task is root
    finally:
        manager._sync_lock.release()
    manager.drain_timeout = 5
    await manager.drain_for_restore()


@pytest.mark.asyncio
@pytest.mark.parametrize("ack_fails", [False, True])
async def test_late_camera_acquisition_is_owned_and_failed_ack_refuses(tmp_path, monkeypatch, ack_fails):
    from backend.app.services import camera_runtime

    inst = instance(tmp_path)
    inst.target_printer_id = 1
    inst._printer_manager = SimpleNamespace(get_client=lambda _: SimpleNamespace(ip_address="127.0.0.1", model="A1"))
    runtime = object.__new__(camera_runtime.WorkerCameraRuntime)
    entered, release = asyncio.Event(), asyncio.Event()

    async def acquire(**kwargs):
        entered.set()
        await release.wait()
        return "owned-camera-lease"

    stop = AsyncMock(side_effect=OSError("worker acknowledgement lost") if ack_fails else None)
    runtime.supervisor = SimpleNamespace(start_raw_proxy=acquire, stop_raw_proxy=stop)
    monkeypatch.setattr(camera_runtime, "get_camera_runtime", lambda: runtime)
    camera = asyncio.create_task(inst.reconcile_camera_proxy())
    await entered.wait()
    manager = VirtualPrinterManager()
    manager._instances[inst.id] = inst
    manager.drain_timeout = 0.02
    with pytest.raises(VPDrainRefused):
        await manager.drain_for_restore()
    assert not camera.done()
    release.set()
    await asyncio.gather(camera, return_exceptions=True)
    manager.drain_timeout = 5
    if ack_fails:
        with pytest.raises(VPDrainRefused):
            await manager.drain_for_restore()
        assert inst._worker_camera_lease == "owned-camera-lease"
    else:
        outcome = await manager.drain_for_restore()
        assert outcome["camera_leases_released"] == 1
        stop.assert_awaited_once_with("owned-camera-lease")


@pytest.mark.asyncio
async def test_replaced_timer_cleanup_is_retained_until_actual_finish(tmp_path, monkeypatch):
    inst = instance(tmp_path)
    inst._mqtt = SimpleNamespace(set_gcode_state=lambda *a, **kw: None, close_admission=lambda: None, stop=AsyncMock())
    entered, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def timer(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()
            # Repeated cancel must not erase the cleanup handle.
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass

    monkeypatch.setattr(inst, "_delayed_finish_release", timer)
    inst._schedule_finish_release("first")
    await entered.wait()
    first = inst._finish_release_task
    inst._schedule_finish_release("second")
    await cleanup.wait()
    stopping = asyncio.create_task(inst.stop_server())
    await asyncio.sleep(0)
    try:
        assert first in inst._finish_tasks and not stopping.done()
    finally:
        release.set()
        await stopping
    assert first.done() and not inst._finish_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["ftp", "mqtt"])
async def test_late_real_listener_bind_is_closed_and_joined(tmp_path, monkeypatch, protocol):
    cert, key = certificates(tmp_path)
    inst = instance(tmp_path)
    if protocol == "ftp":
        service = VirtualPrinterFTPServer(
            tmp_path / "uploads", "12345678", cert, key, port=0, bind_address="127.0.0.1", activities=inst.activities
        )
        inst._ftp = service
    else:
        service = SimpleMQTTServer(
            "01P00A000000000", "12345678", cert, key, port=0, bind_address="127.0.0.1", activities=inst.activities
        )
        inst._mqtt = service
    real_start = asyncio.start_server
    entered, release = asyncio.Event(), asyncio.Event()
    listeners = []

    async def delayed_bind(*args, **kwargs):
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
        listener = await real_start(*args, **kwargs)
        listeners.append(listener)
        return listener

    monkeypatch.setattr(asyncio, "start_server", delayed_bind)
    inst._tasks = [asyncio.create_task(service.start())]
    await entered.wait()
    manager = VirtualPrinterManager()
    manager._instances[inst.id] = inst
    manager.drain_timeout = 0.02
    try:
        with pytest.raises(VPDrainRefused):
            await manager.drain_for_restore()
        assert not manager._drain_task.done()
    finally:
        release.set()
        manager.drain_timeout = 5
        await manager.drain_for_restore()
    assert listeners and all(not server.sockets for server in listeners)
    assert not inst._tasks


@pytest.mark.asyncio
@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="Linux file-descriptor proof")
async def test_linux_socket_fd_count_returns_to_baseline(tmp_path):
    before = len(os.listdir("/proc/self/fd"))
    for tls in (False, True):
        await test_real_proxy_persistent_stream_cut_counted_and_joined(tmp_path / str(tls), tls)
    await asyncio.sleep(0)
    assert len(os.listdir("/proc/self/fd")) <= before


@pytest.mark.asyncio
async def test_unconsumed_option_warning_evidence_stays_bounded(tmp_path):
    from backend.app.services.virtual_printer.manager import _SLICER_OPTIONS_CACHE_LIMIT

    inst = instance(tmp_path)
    for number in range(_SLICER_OPTIONS_CACHE_LIMIT * 3):
        await inst.on_print_command("part", {"file": f"part-{number}.3mf"})
    assert len(inst._options_seen) == _SLICER_OPTIONS_CACHE_LIMIT
    assert inst._options_seen == set(inst._slicer_print_options)
