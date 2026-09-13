"""VP startup failures must release listeners and the real-printer bridge."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.app.services.virtual_printer import manager as vp_manager
from backend.app.services.virtual_printer.mqtt_bridge import MQTTBridge
from backend.app.services.virtual_printer.tcp_proxy import TCPProxy


@pytest.fixture
def startup(tmp_path, monkeypatch):
    services = []
    modes = {}
    entered = asyncio.Event()
    client = SimpleNamespace(
        ip_address="192.0.2.20",
        model="P2S",
        serial_number="22E000000000001",
        state=SimpleNamespace(connected=False),
        register_raw_message_handler=Mock(),
        unregister_raw_message_handler=Mock(),
    )

    class Listener:
        def __init__(self, name, **kwargs):
            self.name = name
            self.bind_address = kwargs.get("bind_address", "192.0.2.10")
            self.ready = asyncio.Event()
            self.closed = False
            services.append(self)

        async def start(self):
            entered.set()
            try:
                mode = modes.get(self.name, "ready")
                if mode == "exit":  # Real services catch their own bind error.
                    return
                if mode == "raise":
                    raise OSError("simulated bind failure")
                if mode != "stall":
                    self.ready.set()
                if mode == "ready_exit":
                    return
                await asyncio.Event().wait()
            finally:
                await self.stop()

        async def stop(self):
            self.closed = True
            self.ready.clear()

        def set_bridge(self, bridge):
            self.bridge = bridge

    for name in ("VirtualPrinterFTPServer", "SimpleMQTTServer", "BindServer", "VirtualPrinterSSDPServer", "TCPProxy"):
        # TCPProxy receives its own name keyword.
        def factory(_name=name, **kwargs):
            kwargs.pop("name", None)
            return Listener(_name, **kwargs)

        monkeypatch.setattr(vp_manager, name, factory)

    instance = vp_manager.VirtualPrinterInstance(
        vp_id=7,
        name="Startup VP",
        mode="file_manager",
        model="N7",
        access_code="12345678",
        serial_suffix="391800007",
        bind_ip="192.0.2.10",
        target_printer_id=24,
        printer_manager=SimpleNamespace(get_client=lambda _: client),
        base_dir=tmp_path,
    )
    monkeypatch.setattr(
        instance, "_resolve_cert_and_advertise", lambda: (tmp_path / "cert", tmp_path / "key", instance.bind_ip)
    )
    return SimpleNamespace(instance=instance, services=services, modes=modes, entered=entered, client=client)


def assert_stopped(startup):
    assert not startup.instance.get_status()["running"]
    assert not startup.instance._tasks
    assert startup.instance._mqtt_bridge is None
    assert all(service.closed and not service.ready.is_set() for service in startup.services)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("service", "mode"),
    [
        ("VirtualPrinterFTPServer", "exit"),
        ("SimpleMQTTServer", "raise"),
        ("BindServer", "ready_exit"),
        ("VirtualPrinterSSDPServer", "exit"),
        ("TCPProxy", "exit"),
    ],
)
async def test_failed_listener_cleans_up_without_attaching_bridge(startup, caplog, service, mode):
    startup.modes[service] = mode
    with caplog.at_level(logging.INFO):
        # An exited listener must fail immediately, not consume the 5 s timeout.
        assert await asyncio.wait_for(startup.instance.start_server(), timeout=1) is False
    assert_stopped(startup)
    startup.client.register_raw_message_handler.assert_not_called()
    assert "Server-mode startup failed" in caplog.text
    assert "services started" not in caplog.text

    # After the configuration is fixed, the same object can start cleanly.
    startup.modes.clear()
    assert await startup.instance.start_server() is True
    assert startup.instance.is_running
    startup.client.register_raw_message_handler.assert_called_once()
    await startup.instance.stop_server()
    startup.client.unregister_raw_message_handler.assert_called_once()
    assert_stopped(startup)


@pytest.mark.asyncio
async def test_camera_readiness_timeout_releases_other_listeners(startup):
    startup.modes["TCPProxy"] = "stall"
    assert await asyncio.wait_for(startup.instance.start_server(), timeout=7) is False
    assert_stopped(startup)
    startup.client.register_raw_message_handler.assert_not_called()


@pytest.mark.asyncio
async def test_cancelled_startup_releases_waiters_and_listeners(startup):
    startup.modes["TCPProxy"] = "stall"
    before = asyncio.all_tasks()
    task = asyncio.create_task(startup.instance.start_server())
    await startup.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert_stopped(startup)
    assert not (asyncio.all_tasks() - before)


@pytest.mark.asyncio
async def test_service_creation_failure_releases_previously_created_tasks(startup, monkeypatch):
    monkeypatch.setattr(vp_manager, "BindServer", Mock(side_effect=RuntimeError("constructor failed")))
    assert await startup.instance.start_server() is False
    assert_stopped(startup)


@pytest.mark.asyncio
async def test_bridge_start_failure_detaches_registered_callback_and_refresh(startup, monkeypatch):
    original_start = MQTTBridge.start
    bridges = []

    async def fail_after_attach(bridge):
        await original_start(bridge)
        bridges.append(bridge)
        raise RuntimeError("failed after attachment")

    monkeypatch.setattr(MQTTBridge, "start", fail_after_attach)
    assert await startup.instance.start_server() is False
    assert_stopped(startup)
    startup.client.register_raw_message_handler.assert_called_once()
    startup.client.unregister_raw_message_handler.assert_called_once()
    assert bridges[0]._refresh_task is None


@pytest.mark.asyncio
async def test_tcp_proxy_readiness_tracks_real_local_listener(monkeypatch):
    # Loopback + OS-assigned port; no connection to a physical printer.
    proxy = TCPProxy("test", 0, "192.0.2.20", 322, bind_address="127.0.0.1")
    outgoing = AsyncMock()
    monkeypatch.setattr(asyncio, "open_connection", outgoing)
    task = asyncio.create_task(proxy.start())
    try:
        await asyncio.wait_for(proxy.ready.wait(), timeout=2)
        assert proxy._server.is_serving()
        outgoing.assert_not_called()
    finally:
        task.cancel()
        await task
    assert not proxy.ready.is_set()
    assert proxy._server is None


@pytest.mark.asyncio
async def test_tcp_proxy_occupied_port_does_not_report_ready():
    async with await asyncio.start_server(lambda reader, writer: writer.close(), "127.0.0.1", 0) as occupied:
        port = occupied.sockets[0].getsockname()[1]
        proxy = TCPProxy("test", port, "192.0.2.20", 322, bind_address="127.0.0.1")
        await asyncio.wait_for(proxy.start(), timeout=2)
        assert not proxy.ready.is_set()
        assert proxy._server is None
