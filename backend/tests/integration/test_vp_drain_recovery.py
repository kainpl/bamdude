"""Recovery after real loopback upload errors; adapted from Opus N1/N2/N3/N7 probes."""

import asyncio
import socket
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.app.services.virtual_printer.certificate import CertificateService
from backend.app.services.virtual_printer.ftp_server import VirtualPrinterFTPServer
from backend.app.services.virtual_printer.lifecycle import Activities, VPDrainRefused
from backend.app.services.virtual_printer.mqtt_server import SimpleMQTTServer

LOOP = "127.0.0.1"
CODE = "probe123"


def certs(tmp_path):
    service = CertificateService(cert_dir=tmp_path / "printer", serial="01P00A000000000", shared_ca_dir=tmp_path / "ca")
    return service.generate_certificates()


def client_tls():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def port_of(server):
    return server.sockets[0].getsockname()[1]


async def _line(reader):
    return (await asyncio.wait_for(reader.readline(), 5)).decode().strip()


async def _ftp_server(tmp_path, activities):
    cert, key = certs(tmp_path)
    ftp = VirtualPrinterFTPServer(
        upload_dir=tmp_path / "uploads",
        access_code=CODE,
        cert_path=cert,
        key_path=key,
        port=0,
        bind_address=LOOP,
        activities=activities,
    )
    service = asyncio.create_task(ftp.start())
    await asyncio.wait_for(ftp.ready.wait(), 5)
    return ftp, service


async def _login_and_open_data(port):
    reader, writer = await asyncio.open_connection(LOOP, port, ssl=client_tls())
    assert (await _line(reader)).startswith("220")
    for cmd, code in (("USER bblp", "331"), (f"PASS {CODE}", "230"), ("TYPE I", "200")):
        writer.write(f"{cmd}\r\n".encode())
        await writer.drain()
        assert (await _line(reader)).startswith(code)
    writer.write(b"EPSV\r\n")
    await writer.drain()
    resp = await _line(reader)
    assert resp.startswith("229"), resp
    data_port = int(resp.split("|||")[1].split("|")[0])
    dreader, dwriter = await asyncio.open_connection(LOOP, data_port, ssl=client_tls())
    return reader, writer, dreader, dwriter


async def _interrupted_upload(port):
    """A slicer that starts STOR, sends part of the file, then drops (laptop sleep / cancel)."""
    reader, writer, dreader, dwriter = await _login_and_open_data(port)
    writer.write(b"STOR job.3mf\r\n")
    await writer.drain()
    assert (await _line(reader)).startswith("150")
    dwriter.write(b"PK\x03\x04" + b"x" * 4096)
    await dwriter.drain()
    await asyncio.sleep(0.2)
    writer.transport.abort()  # control socket gone first
    await asyncio.sleep(0.3)
    dwriter.transport.abort()  # then the data socket


async def _completed_upload(port, payload):
    reader, writer, dreader, dwriter = await _login_and_open_data(port)
    writer.write(b"STOR job.3mf\r\n")
    await writer.drain()
    assert (await _line(reader)).startswith("150")
    dwriter.write(payload)
    await dwriter.drain()
    dwriter.close()
    await dwriter.wait_closed()
    assert (await _line(reader)).startswith("226")
    writer.write(b"QUIT\r\n")
    await writer.drain()
    writer.close()


def _valid_3mf(tmp_path):
    import zipfile

    p = tmp_path / "valid.3mf"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("Metadata/plate_1.gcode", "G28\n")
    return p.read_bytes()


async def _settle(activities, seconds=3.0):
    t0 = time.monotonic()
    while activities._business and time.monotonic() - t0 < seconds:
        await asyncio.sleep(0.05)


async def test_interrupted_upload_finishes_and_ftp_stop_is_repeatable(tmp_path):
    record = {}
    # Control: a completed upload leaves the owner healthy and stop() is proven.
    ok = Activities()
    ftp_ok, svc_ok = await _ftp_server(tmp_path / "ok", ok)
    await _completed_upload(port_of(ftp_ok._server), _valid_3mf(tmp_path))
    await _settle(ok)
    record["control_failed_flag"] = getattr(ok, "failed", False)
    await asyncio.wait_for(ftp_ok.stop(), 5)
    record["control_stop"] = "proven"
    svc_ok.cancel()
    await asyncio.gather(svc_ok, return_exceptions=True)

    bad = Activities()
    ftp, svc = await _ftp_server(tmp_path / "bad", bad)
    await _interrupted_upload(port_of(ftp._server))
    await _settle(bad)
    record["after_interrupted_upload"] = {"business_running": bad._business, "failed": getattr(bad, "failed", False)}
    sessions_before = len(ftp._sessions)
    outcomes = []
    for _ in range(2):  # a later retry must remain proven, with nothing running
        try:
            await asyncio.wait_for(ftp.stop(), 5)
            outcomes.append("proven")
        except VPDrainRefused as exc:
            outcomes.append(exc.reason)
        await asyncio.sleep(0.5)
    record["stop_outcomes"] = outcomes
    record["sessions_tracked_before"] = sessions_before
    record["server_closed_but_not_joined"] = ftp._server is not None
    print("N1N2", record)
    svc.cancel()
    await asyncio.gather(svc, return_exceptions=True)
    assert record["control_failed_flag"] is False
    assert bad._business == 0 and getattr(bad, "failed", False) is False
    assert outcomes == ["proven", "proven"]
    assert not ftp.connections.handlers and not ftp._sessions and ftp._server is None


async def test_failed_business_allows_both_edits_and_restore(test_engine, db_session, tmp_path, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from backend.app.models.virtual_printer import VirtualPrinter
    from backend.app.services.virtual_printer.manager import VirtualPrinterInstance, VirtualPrinterManager

    def row(name, pos, suffix):
        return VirtualPrinter(
            name=name,
            enabled=True,
            mode="file_manager",
            model="N2S",
            access_code="12345678",
            serial_suffix=suffix,
            bind_ip="127.0.0.1",
            position=pos,
        )

    a, b = row("A", 0, "11111111"), row("B", 1, "22222222")
    db_session.add_all([a, b])
    await db_session.commit()
    starts = []

    async def start(instance):
        starts.append((instance.name, instance.model))
        return True

    monkeypatch.setattr(VirtualPrinterInstance, "start_server", start)

    async def run(fail):
        starts.clear()
        manager = VirtualPrinterManager()
        manager._base_dir = tmp_path / ("fail" if fail else "ok")
        manager.set_session_factory(async_sessionmaker(test_engine, expire_on_commit=False))
        manager.drain_timeout = 2
        a.model, b.model = "N2S", "N2S"
        await db_session.commit()
        await manager.sync_from_db()
        inst_a = manager.get_instance(a.id)
        try:  # one upload that ended with an exception (N1's path), long before any edit
            with inst_a.activities.business():
                if fail:
                    raise ConnectionResetError("slicer dropped")
        except ConnectionResetError:
            pass
        # Edit both VPs; B is ordered after A.
        a.model, b.model = "N1", "N1"
        await db_session.commit()
        await manager.sync_from_db()
        await asyncio.sleep(0.2)
        after_edit = list(starts)
        removed = await manager.remove_instance(a.id)
        drains = []
        for _ in range(2):
            try:
                await manager.drain_for_restore()
                drains.append("proven")
            except VPDrainRefused as exc:
                drains.append(exc.reason)
        return {
            "starts": after_edit,
            "a_retiring_retained": a.id in manager._retiring,
            "remove_instance": removed,
            "drain": drains,
            "sync_task_exception": type(manager._sync_task.exception()).__name__
            if manager._sync_task and manager._sync_task.done() and manager._sync_task.exception()
            else None,
        }

    control = await run(False)
    failed = await run(True)
    print("N3", {"control": control, "after_one_failed_upload": failed})
    assert ("A", "N1") in control["starts"] and ("B", "N1") in control["starts"] and control["drain"][0] == "proven"
    assert ("A", "N1") in failed["starts"]
    assert ("B", "N1") in failed["starts"]
    assert failed["a_retiring_retained"] is False
    assert failed["remove_instance"] is True
    assert failed["drain"] == ["proven", "proven"]


class _SleepingTLSPeer(threading.Thread):
    """Blocking TLS client that completes the handshake, optionally sends bytes, then never reads."""

    def __init__(self, port, first=b""):
        super().__init__(daemon=True)
        self.port, self.first = port, first
        self.ready = threading.Event()
        self.stop = threading.Event()

    def run(self):
        raw = socket.create_connection((LOOP, self.port))
        tls = client_tls().wrap_socket(raw, server_hostname="vp")
        if self.first:
            tls.sendall(self.first)
        self.ready.set()
        self.stop.wait(60)  # asleep: no read, no close_notify
        raw.close()


def _connect_packet(access_code):
    def s(b):
        return len(b).to_bytes(2, "big") + b

    body = s(b"MQTT") + bytes([4, 0xC2]) + (60).to_bytes(2, "big") + s(b"probe") + s(b"bblp") + s(access_code.encode())
    return bytes([0x10, len(body)]) + body


async def test_n4_idle_and_sleeping_peers_are_bounded(tmp_path):
    cert, key = certs(tmp_path)
    record = {}
    # FTP: idle authenticated control session.
    ftp = VirtualPrinterFTPServer(
        upload_dir=tmp_path / "u", access_code=CODE, cert_path=cert, key_path=key, port=0, bind_address=LOOP
    )
    fsvc = asyncio.create_task(ftp.start())
    await asyncio.wait_for(ftp.ready.wait(), 5)
    r, w = await asyncio.open_connection(LOOP, port_of(ftp._server), ssl=client_tls())
    assert (await _line(r)).startswith("220")
    t0 = time.monotonic()
    await asyncio.wait_for(ftp.stop(), 10)
    record["ftp_idle_control_stop_s"] = round(time.monotonic() - t0, 2)
    fsvc.cancel()
    await asyncio.gather(fsvc, return_exceptions=True)

    # MQTT: CONNECTed asyncio client + pre-CONNECT asyncio client + two sleeping blocking TLS peers.
    mqtt = SimpleMQTTServer(
        serial="01P00A000000000", access_code=CODE, cert_path=cert, key_path=key, port=0, bind_address=LOOP
    )
    msvc = asyncio.create_task(mqtt.start())
    await asyncio.wait_for(mqtt.ready.wait(), 10)
    port = port_of(mqtt._server)
    cr, cw = await asyncio.open_connection(LOOP, port, ssl=client_tls())
    cw.write(_connect_packet(CODE))
    await cw.drain()
    assert await asyncio.wait_for(cr.readexactly(4), 3) == bytes([0x20, 0x02, 0x00, 0x00])
    pr, pw = await asyncio.open_connection(LOOP, port, ssl=client_tls())
    sleepers = [_SleepingTLSPeer(port), _SleepingTLSPeer(port, _connect_packet(CODE))]
    for s in sleepers:
        s.start()
        assert await asyncio.to_thread(s.ready.wait, 5)  # never block the server's loop
    await asyncio.sleep(0.3)
    record["mqtt_handlers_tracked"] = len(mqtt.connections.handlers)
    t0 = time.monotonic()
    await asyncio.wait_for(mqtt.stop(), 15)
    record["mqtt_mixed_peers_stop_s"] = round(time.monotonic() - t0, 2)
    record["mqtt_cuts_counted"] = mqtt.connections.cuts
    for s in sleepers:
        s.stop.set()
    msvc.cancel()
    await asyncio.gather(msvc, return_exceptions=True)
    print("N4", record)
    assert record["ftp_idle_control_stop_s"] < 3
    assert record["mqtt_handlers_tracked"] == 4
    assert record["mqtt_mixed_peers_stop_s"] < 5


async def test_historical_failed_sync_does_not_block_restore(test_engine, db_session, tmp_path, monkeypatch):
    """An ordinary edit whose sync read failed once (e.g. a database lock), long before any restore."""
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from backend.app.models.virtual_printer import VirtualPrinter
    from backend.app.services.virtual_printer.manager import VirtualPrinterInstance, VirtualPrinterManager

    db_session.add(
        VirtualPrinter(
            name="A",
            enabled=True,
            mode="file_manager",
            model="N2S",
            access_code="12345678",
            serial_suffix="33333333",
            bind_ip="127.0.0.1",
        )
    )
    await db_session.commit()

    async def start(instance):
        return True

    monkeypatch.setattr(VirtualPrinterInstance, "start_server", start)
    fail_next = {"on": False}

    class FlakySession(AsyncSession):
        async def execute(self, *a, **k):
            if fail_next["on"]:
                fail_next["on"] = False
                raise OperationalError("SELECT", {}, Exception("database is locked"))
            return await super().execute(*a, **k)

    async def run(fail):
        manager = VirtualPrinterManager()
        manager._base_dir = tmp_path / ("f" if fail else "c")
        manager.set_session_factory(async_sessionmaker(test_engine, class_=FlakySession, expire_on_commit=False))
        manager.drain_timeout = 2
        await manager.sync_from_db()  # VP A running
        fail_next["on"] = fail
        await manager.sync_from_db()  # an ordinary edit's sync
        inst = next(iter(manager._instances.values()))
        badge = inst.get_status()["runtime_state"]
        drains = []
        for _ in range(2):
            try:
                await manager.drain_for_restore()
                drains.append("proven")
            except VPDrainRefused as exc:
                drains.append(exc.reason)
        return {"badge_after_edit": badge, "drain": drains}

    control, failed = await run(False), await run(True)
    print("N7", {"control": control, "after_one_failed_sync": failed})
    assert control["drain"][0] == "proven"
    assert failed["badge_after_edit"] == "running"
    assert failed["drain"] == ["proven", "proven"]


def _publish_packet(topic, payload):
    body = len(topic).to_bytes(2, "big") + topic + payload
    return bytes([0x30, len(body)]) + body


async def test_non_object_json_publish_does_not_poison_mqtt_stop(tmp_path):
    """Secondary trigger: an authenticated publish whose JSON root is not an object (TypeError)."""
    cert, key = certs(tmp_path)
    activities = Activities()
    mqtt = SimpleMQTTServer(
        serial="01P00A000000000",
        access_code=CODE,
        cert_path=cert,
        key_path=key,
        port=0,
        bind_address=LOOP,
        activities=activities,
    )
    msvc = asyncio.create_task(mqtt.start())
    await asyncio.wait_for(mqtt.ready.wait(), 10)
    r, w = await asyncio.open_connection(LOOP, port_of(mqtt._server), ssl=client_tls())
    w.write(_connect_packet(CODE))
    await w.drain()
    assert await asyncio.wait_for(r.readexactly(4), 3) == bytes([0x20, 0x02, 0x00, 0x00])
    topic = b"device/01P00A000000000/request"
    w.write(_publish_packet(topic, b'{"pushing": {"command": "pushall"}}'))  # control: a normal command
    await w.drain()
    await asyncio.sleep(0.5)
    control_failed = getattr(activities, "failed", False)
    w.write(_publish_packet(topic, b'"pushing"'))  # valid JSON, string root
    await w.drain()
    await asyncio.sleep(0.5)
    poisoned = getattr(activities, "failed", False)
    try:
        await asyncio.wait_for(mqtt.stop(), 10)
        stop = "proven"
    except VPDrainRefused as exc:
        stop = exc.reason
    msvc.cancel()
    await asyncio.gather(msvc, return_exceptions=True)
    print("N1m", {"control_failed": control_failed, "after_string_root_publish_failed": poisoned, "stop": stop})
    assert control_failed is False and poisoned is False and stop == "proven"


async def test_n5_pre_handshake_tcp_peer_is_bounded_by_the_native_cap(tmp_path):
    cert, key = certs(tmp_path)
    mqtt = SimpleMQTTServer(
        serial="01P00A000000000", access_code=CODE, cert_path=cert, key_path=key, port=0, bind_address=LOOP
    )
    msvc = asyncio.create_task(mqtt.start())
    await asyncio.wait_for(mqtt.ready.wait(), 10)
    raw = socket.create_connection((LOOP, port_of(mqtt._server)))  # TCP only, no ClientHello
    await asyncio.sleep(0.2)
    tracked = len(mqtt.connections.handlers)
    t0 = time.monotonic()
    await asyncio.wait_for(mqtt.stop(), 20)
    elapsed = round(time.monotonic() - t0, 2)
    raw.close()
    msvc.cancel()
    await asyncio.gather(msvc, return_exceptions=True)
    # Characterise the cap for a legitimate client: a normal handshake time on loopback.
    mqtt2 = SimpleMQTTServer(
        serial="01P00A000000000", access_code=CODE, cert_path=cert, key_path=key, port=0, bind_address=LOOP
    )
    msvc2 = asyncio.create_task(mqtt2.start())
    await asyncio.wait_for(mqtt2.ready.wait(), 10)
    t1 = time.monotonic()
    _, w = await asyncio.open_connection(LOOP, port_of(mqtt2._server), ssl=client_tls())
    handshake = round(time.monotonic() - t1, 3)
    w.transport.abort()
    await asyncio.wait_for(mqtt2.stop(), 10)
    msvc2.cancel()
    await asyncio.gather(msvc2, return_exceptions=True)
    print("N5", {"pre_handshake_tracked_by_handlers": tracked, "stop_s": elapsed, "loopback_handshake_s": handshake})
    assert tracked == 0 and 4.0 <= elapsed < 7.0


async def test_n6_scheduled_local_backup_busy_keeps_its_slot(tmp_path, monkeypatch):
    from backend.app.api.routes import settings as settings_routes
    from backend.app.services import backup_files
    from backend.app.services.local_backup import LocalBackupService

    conf = {"enabled": True, "schedule": "daily", "time": "03:00", "retention": 5, "path": str(tmp_path / "backups")}

    async def load_settings():
        return dict(conf)

    service = LocalBackupService()
    monkeypatch.setattr(service, "_load_settings", load_settings)
    now = datetime.now(timezone.utc)
    due = now - timedelta(minutes=1)
    service._next_run = due
    async with backup_files.exclusive_operation():
        await service._check_scheduled_backup()
    busy = (service._last_status, service._next_run)

    async def fake_zip(output_path=None):
        target = Path(output_path) / "bamdude-backup-probe.zip"
        target.write_bytes(b"PK")
        return target, target.name

    monkeypatch.setattr(settings_routes, "create_backup_zip", fake_zip)
    await service._check_scheduled_backup()  # next loop pass, guard free
    print(
        "N6",
        {
            "busy_status": busy[0],
            "busy_slot_kept": busy[1] == due,
            "retry_status": service._last_status,
            "retry_moved_slot": service._next_run > now,
        },
    )
    assert busy[0] != "failed" and busy[1] == due
    assert service._last_status == "success" and service._next_run > now
