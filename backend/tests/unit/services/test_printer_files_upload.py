"""Upload across both transports, and the wire name that stays put."""

import asyncio
import inspect

import pytest

from backend.app.services.printer_files.base import PrinterFileTransport
from backend.app.services.printer_files.ftp import FtpTransport
from backend.app.services.printer_files.tunnel import TunnelTransport
from backend.tests.tunnel_fixtures import FakeTunnelServer


class _Printer:
    id = 1
    ip_address = "127.0.0.1"
    access_code = "12345678"
    model = "X2D"


def _tunnel_to(host: str, port: int) -> TunnelTransport:
    async def connector():
        return await asyncio.open_connection(host, port)

    return TunnelTransport(_Printer(), port=port, connector=connector)


def test_the_protocol_now_declares_upload():
    """Stage 1 left it out because only one side could honour it. Both can."""
    assert hasattr(PrinterFileTransport, "upload")
    assert "upload" in dir(FtpTransport)
    assert "upload" in dir(TunnelTransport)


def test_both_implementations_take_the_same_arguments():
    ftp = inspect.signature(FtpTransport.upload).parameters
    tunnel = inspect.signature(TunnelTransport.upload).parameters
    assert list(ftp) == list(tunnel)


@pytest.mark.asyncio
async def test_the_tunnel_transport_uploads_to_emmc(tmp_path):
    """⚠️ `emmc` on upload, `internal` on listing — the same medium, two wire
    names, and both live only inside this class."""
    server = FakeTunnelServer()
    host, port = await server.start()
    try:
        local = tmp_path / "job.gcode.3mf"
        local.write_bytes(b"PK\x03\x04" + b"x" * 5000)

        assert await _tunnel_to(host, port).upload(local, "job.gcode.3mf") is True
        assert server.upload_meta["job.gcode.3mf"]["storage"] == "emmc"
        assert server.uploads["job.gcode.3mf"] == local.read_bytes()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_a_refused_upload_is_false_not_an_exception(tmp_path):
    """The dispatcher turns False into its own message; an exception escaping
    here would bypass that and read as a BamDude crash."""
    server = FakeTunnelServer()
    server.fail_next_with = 7
    host, port = await server.start()
    try:
        local = tmp_path / "job.gcode.3mf"
        local.write_bytes(b"x" * 100)

        assert await _tunnel_to(host, port).upload(local, "job.gcode.3mf") is False
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_a_cancellation_from_the_callback_is_not_swallowed(tmp_path):
    """⚠️ Only TunnelError is caught. The dispatcher cancels a job from inside
    the progress callback, and turning that into `False` would report a
    cancelled job as a failed transfer."""

    class Cancelled(Exception):
        pass

    def cancel_now(_sent, _total):
        raise Cancelled

    server = FakeTunnelServer()
    host, port = await server.start()
    try:
        local = tmp_path / "job.gcode.3mf"
        local.write_bytes(b"x" * 100)

        with pytest.raises(Cancelled):
            await _tunnel_to(host, port).upload(local, "job.gcode.3mf", cancel_now)
    finally:
        await server.stop()
