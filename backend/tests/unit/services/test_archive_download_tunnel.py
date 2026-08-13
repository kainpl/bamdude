"""An externally started print on a cardless machine still gets its 3MF.

Without this the archive keeps file_path="" and no_3mf_available=True for ever,
across all four of archive_download_retry's triggers — on a printer whose file
is sitting right there in internal storage.
"""

import asyncio

import pytest

from backend.app.services.archive_download import _try_internal_storage
from backend.tests.tunnel_fixtures import FakeTunnelServer, listing_entry


class _Printer:
    id = 1
    name = "X2D"
    ip_address = "127.0.0.1"
    access_code = "12345678"
    model = "X2D"


class _State:
    def __init__(self, internal: bool):
        self.sdcard_state = 0
        self.print_option_support = {"model_internal_storage": internal, "print_with_emmc": internal}


def _patch_transport(monkeypatch, host: str, port: int, *, internal: bool = True):
    from backend.app.services.printer_files.tunnel import TunnelTransport

    def _factory(printer, _storage):
        async def connector():
            return await asyncio.open_connection(host, port)

        return TunnelTransport(printer, port=port, connector=connector)

    monkeypatch.setattr("backend.app.services.printer_files.factory.transport_for", _factory)
    monkeypatch.setattr(
        "backend.app.services.printer_manager.printer_manager.get_status",
        lambda _printer_id: _State(internal),
    )


@pytest.mark.asyncio
async def test_the_file_is_recovered_from_internal_storage(tmp_path, monkeypatch):
    server = FakeTunnelServer()
    name = "benchy.gcode.3mf"
    path = f"/userdata/model/history/{name}"
    server.files = [listing_entry(name)]
    server.file_bytes[path] = b"PK\x03\x04tunnel-copy"
    host, port = await server.start()
    try:
        _patch_transport(monkeypatch, host, port)
        result = await _try_internal_storage(_Printer(), [name], tmp_path)

        assert result is not None
        temp_path, downloaded = result
        assert temp_path.read_bytes() == b"PK\x03\x04tunnel-copy"
        assert downloaded == name
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_a_printer_without_internal_storage_is_not_probed(tmp_path, monkeypatch):
    """⚠️ On P1S and A1 mini port 6000 is open and completes a TLS handshake —
    that is the camera. Probing it would cost a timeout on every failed
    recovery for most of the farm."""
    server = FakeTunnelServer()
    server.files = [listing_entry("benchy.gcode.3mf")]
    host, port = await server.start()
    try:
        _patch_transport(monkeypatch, host, port, internal=False)
        assert await _try_internal_storage(_Printer(), ["benchy.gcode.3mf"], tmp_path) is None
        assert server.requests == []
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_a_name_that_is_not_there_returns_nothing(tmp_path, monkeypatch):
    server = FakeTunnelServer()
    server.files = [listing_entry("something-else.gcode.3mf")]
    host, port = await server.start()
    try:
        _patch_transport(monkeypatch, host, port)
        assert await _try_internal_storage(_Printer(), ["benchy.gcode.3mf"], tmp_path) is None
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_the_match_is_case_insensitive(tmp_path, monkeypatch):
    """The printer's own listing does not have to spell the name the way our
    archive does."""
    server = FakeTunnelServer()
    name = "Benchy.GCode.3mf"
    server.files = [listing_entry(name)]
    server.file_bytes[f"/userdata/model/history/{name}"] = b"PK\x03\x04x"
    host, port = await server.start()
    try:
        _patch_transport(monkeypatch, host, port)
        result = await _try_internal_storage(_Printer(), ["benchy.gcode.3mf"], tmp_path)
        assert result is not None
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_an_empty_read_is_not_written_as_a_file(tmp_path, monkeypatch):
    """A zero-byte 3MF attached to an archive is worse than none — it looks
    like a recovery that worked."""
    server = FakeTunnelServer()
    name = "benchy.gcode.3mf"
    server.files = [listing_entry(name)]
    # No file_bytes entry: the read comes back empty.
    host, port = await server.start()
    try:
        _patch_transport(monkeypatch, host, port)
        assert await _try_internal_storage(_Printer(), [name], tmp_path) is None
    finally:
        await server.stop()
