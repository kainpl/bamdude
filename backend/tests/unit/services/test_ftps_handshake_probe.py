"""The diagnostic asks port 990 for TLS, not just for a TCP accept (upstream 91acac2b).

A bare TCP connect to 990 passed for a printer whose file service answered in
plain text — the port was green while every archive came back empty. The probe
now completes an implicit-TLS handshake the way the FTP client does (the model's
TLS cap included), and nothing more: no login, so the pre-save Add Printer flow
can run it without an access code.

Run against a real socket that answers with a plaintext FTP banner, which is
what reproduces ``WRONG_VERSION_NUMBER`` — not a mocked ``ssl``.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.app.services import printer_diagnostic


async def _plaintext_server():
    async def handle(_reader, writer):
        writer.write(b"220 Service not available, closing control connection.\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


@pytest.mark.asyncio
async def test_a_plaintext_answer_is_no_tls(monkeypatch):
    server, port = await _plaintext_server()
    monkeypatch.setattr(printer_diagnostic, "PORT_FTPS", port)
    try:
        assert await printer_diagnostic._ftps_handshake("127.0.0.1", "X1C") == "no_tls"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_a_completed_handshake_is_ok(monkeypatch):
    class _Writer:
        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def handshake_done(*_args, **_kwargs):
        return object(), _Writer()

    monkeypatch.setattr(printer_diagnostic.asyncio, "open_connection", handshake_done)
    assert await printer_diagnostic._ftps_handshake("192.0.2.1", "X1C") == "ok"


@pytest.mark.asyncio
async def test_the_models_tls_cap_is_applied(monkeypatch):
    """A pass has to mean the FTP client would get through too."""
    import ssl

    seen = {}

    async def capture(*_args, ssl=None, **_kwargs):
        seen["max"] = ssl.maximum_version
        raise ConnectionRefusedError

    monkeypatch.setattr(printer_diagnostic.asyncio, "open_connection", capture)
    monkeypatch.setattr(printer_diagnostic, "get_ftp_profile", lambda _model: type("P", (), {"cap_tls_v1_2": True})())
    await printer_diagnostic._ftps_handshake("192.0.2.1", "P2S")
    assert seen["max"] == ssl.TLSVersion.TLSv1_2
