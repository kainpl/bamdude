"""Expected peer disconnects stay quiet; unexpected workflow errors keep their traceback."""

import asyncio
import logging
import ssl
import time

import pytest

from backend.app.services.virtual_printer.certificate import CertificateService
from backend.app.services.virtual_printer.ftp_server import VirtualPrinterFTPServer
from backend.app.services.virtual_printer.lifecycle import Activities, VPDrainRefused
from backend.app.services.virtual_printer.mqtt_server import SimpleMQTTServer
from backend.tests.unit.services.test_vp_owned_drain import connect_packet

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


async def _interrupted_upload(port):
    reader, writer = await asyncio.open_connection(LOOP, port, ssl=client_tls())
    assert (await _line(reader)).startswith("220")
    for cmd, code in (("USER bblp", "331"), (f"PASS {CODE}", "230"), ("TYPE I", "200")):
        writer.write(f"{cmd}\r\n".encode())
        await writer.drain()
        assert (await _line(reader)).startswith(code)
    writer.write(b"EPSV\r\n")
    await writer.drain()
    resp = await _line(reader)
    data_port = int(resp.split("|||")[1].split("|")[0])
    _, dwriter = await asyncio.open_connection(LOOP, data_port, ssl=client_tls())
    writer.write(b"STOR job.3mf\r\n")
    await writer.drain()
    assert (await _line(reader)).startswith("150")
    dwriter.write(b"PK\x03\x04" + b"x" * 4096)
    await dwriter.drain()
    await asyncio.sleep(0.2)
    writer.transport.abort()  # control socket gone first: the 426 has nowhere to go
    await asyncio.sleep(0.3)
    dwriter.transport.abort()


async def test_f1_interrupted_upload_no_longer_poisons_ftp_stop(tmp_path, caplog):
    caplog.set_level(logging.ERROR)
    activities = Activities()
    ftp, svc = await _ftp_server(tmp_path, activities)
    await _interrupted_upload(port_of(ftp._server))
    t0 = time.monotonic()
    while activities._business and time.monotonic() - t0 < 3:
        await asyncio.sleep(0.05)
    outcomes = []
    for _ in range(2):
        try:
            await asyncio.wait_for(ftp.stop(), 5)
            outcomes.append("proven")
        except VPDrainRefused as exc:
            outcomes.append(exc.reason)
    logged = [r for r in caplog.records if "activity ended with an error" in r.getMessage()]
    svc.cancel()
    await asyncio.gather(svc, return_exceptions=True)
    record = {
        "business_after": activities._business,
        "stop_outcomes": outcomes,
        "sessions_left": len(ftp._sessions),
        "error_logged_with_traceback": bool(logged and logged[0].exc_info),
        "partial_file_left": (tmp_path / "uploads" / "job.3mf").exists(),
    }
    assert not record["error_logged_with_traceback"]
    assert not any(r.levelno >= logging.ERROR and r.exc_info for r in caplog.records)
    assert record["business_after"] == 0
    assert outcomes == ["proven", "proven"]
    assert not record["partial_file_left"]


@pytest.mark.parametrize("error", [RuntimeError, OSError])
def test_unexpected_business_failure_still_has_traceback(caplog, error):
    with caplog.at_level(logging.ERROR), pytest.raises(error), Activities().business():
        raise error("unexpected workflow defect")
    assert any(r.exc_info and "activity ended with an error" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("block", ["print", "pushing", "info"])
async def test_non_object_mqtt_blocks_keep_the_session_usable_without_traceback(tmp_path, caplog, monkeypatch, block):
    caplog.set_level(logging.ERROR)
    cert, key = certs(tmp_path)
    mqtt = SimpleMQTTServer("01P00A000000000", "12345678", cert, key, port=0, bind_address=LOOP)
    processed = asyncio.Event()
    process_publish = mqtt._process_publish

    async def observed_publish(*args):
        try:
            await process_publish(*args)
        finally:
            processed.set()

    monkeypatch.setattr(mqtt, "_process_publish", observed_publish)
    service = asyncio.create_task(mqtt.start())
    await asyncio.wait_for(mqtt.ready.wait(), 5)
    reader, writer = await asyncio.open_connection(LOOP, port_of(mqtt._server), ssl=client_tls())

    async def publish(payload):
        topic = b"device/01P00A000000000/request"
        body = len(topic).to_bytes(2, "big") + topic + payload
        writer.write(bytes([0x30, len(body)]) + body)
        await writer.drain()

    try:
        writer.write(connect_packet())
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(4), 5) == b"\x20\x02\x00\x00"
        await publish(f'{{"{block}":"x"}}'.encode())
        await asyncio.wait_for(processed.wait(), 5)
        assert not any(r.levelno >= logging.ERROR for r in caplog.records)
        await publish(b'{"pushing":{"command":"pushall"}}')
        assert await asyncio.wait_for(reader.readexactly(1), 5) == b"\x30"
        assert not any(r.levelno >= logging.ERROR for r in caplog.records)
    finally:
        writer.transport.abort()
        await asyncio.wait_for(mqtt.stop(), 5)
        service.cancel()
        await asyncio.gather(service, return_exceptions=True)
