"""Two transports, one narrow protocol, and the wire vocabulary staying put."""

import asyncio
from datetime import datetime

import pytest

from backend.app.services.bambu_ftp import DeleteResult
from backend.app.services.printer_files.base import RemoteFile
from backend.app.services.printer_files.factory import transport_for
from backend.app.services.printer_files.ftp import FtpTransport
from backend.app.services.printer_files.tunnel import TunnelTransport
from backend.tests.tunnel_fixtures import FakeTunnelServer, listing_entry


class _Printer:
    id = 1
    ip_address = "127.0.0.1"
    access_code = "12345678"
    model = "X2D"


def _tunnel_to(host: str, port: int) -> TunnelTransport:
    async def connector():
        return await asyncio.open_connection(host, port)

    return TunnelTransport(_Printer(), port=port, connector=connector)


def test_remote_file_serialises_to_the_shape_the_frontend_already_types():
    entry = RemoteFile(name="a.3mf", path="/a.3mf", size=7, mtime=datetime(2026, 8, 13, 10, 0))
    as_dict = entry.as_dict()
    assert as_dict["name"] == "a.3mf"
    assert as_dict["path"] == "/a.3mf"
    assert as_dict["size"] == 7
    assert as_dict["is_directory"] is False
    assert as_dict["mtime"] == datetime(2026, 8, 13, 10, 0)


def test_a_file_with_no_mtime_omits_the_key():
    """The FTP listing only sometimes parses a date, and the existing API omits
    the key rather than sending null — keep that contract."""
    assert "mtime" not in RemoteFile(name="a", path="/a", size=0).as_dict()


def test_the_factory_picks_the_transport_from_the_storage_name():
    assert isinstance(transport_for(_Printer(), "external"), FtpTransport)
    assert isinstance(transport_for(_Printer(), "internal"), TunnelTransport)


def test_the_factory_rejects_a_wire_name():
    """`emmc` is a wire spelling. It must never reach this far up."""
    with pytest.raises(ValueError):
        transport_for(_Printer(), "emmc")


@pytest.mark.asyncio
async def test_the_tunnel_transport_lists_and_normalises():
    server = FakeTunnelServer()
    server.files = [listing_entry("a.gcode.3mf", size=2629824, time=1786638756)]
    host, port = await server.start()
    try:
        files = await _tunnel_to(host, port).list_files("/")
        assert len(files) == 1
        assert files[0].name == "a.gcode.3mf"
        assert files[0].path == "/userdata/model/history/a.gcode.3mf"
        assert files[0].size == 2629824
        assert files[0].is_directory is False
        assert files[0].mtime is not None
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_the_internal_catalogue_is_flat_so_the_path_is_ignored():
    """There are no directories on internal storage; asking for a subdirectory
    is not an error, it is simply the same flat catalogue."""
    server = FakeTunnelServer()
    server.files = [listing_entry("a.gcode.3mf")]
    host, port = await server.start()
    try:
        deep = await _tunnel_to(host, port).list_files("/nowhere/at/all")
        assert [f.name for f in deep] == ["a.gcode.3mf"]
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_the_wire_storage_name_never_leaves_the_transport():
    """Our vocabulary is external/internal; `internal` is what goes on the wire
    for a listing, and `emmc`/`udisk` appear nowhere above this layer."""
    server = FakeTunnelServer()
    host, port = await server.start()
    try:
        await _tunnel_to(host, port).list_files("/")
        listing = [r for r in server.requests if r.get("cmdtype") == 1][0]
        assert listing["req"]["storage"] == "internal"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_the_catalogue_type_reaches_the_wire():
    """⚠️ On the card a timelapse lives in a directory; over the tunnel it is a
    different catalogue asked for by name. Same idea, two mechanisms."""
    server = FakeTunnelServer()
    host, port = await server.start()
    try:
        await _tunnel_to(host, port).list_files("/", file_type="timelapse")
        listing = [r for r in server.requests if r.get("cmdtype") == 1][0]
        assert listing["req"]["type"] == "timelapse"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_models_are_the_default_catalogue():
    server = FakeTunnelServer()
    host, port = await server.start()
    try:
        await _tunnel_to(host, port).list_files("/")
        listing = [r for r in server.requests if r.get("cmdtype") == 1][0]
        assert listing["req"]["type"] == "model"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_the_ftp_transport_accepts_the_type_and_ignores_it():
    """On the card the path already says which catalogue this is — /timelapse
    is a real directory. Accepting the argument keeps one protocol; acting on
    it would invent a second way to say the same thing."""
    import inspect

    signature = inspect.signature(FtpTransport.list_files)
    assert "file_type" in signature.parameters


@pytest.mark.asyncio
async def test_the_tunnel_transport_reads_bytes():
    server = FakeTunnelServer()
    path = "/userdata/model/history/a.gcode.3mf"
    server.file_bytes[path] = b"PK\x03\x04body"
    host, port = await server.start()
    try:
        assert await _tunnel_to(host, port).read_bytes(path) == b"PK\x03\x04body"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_a_refused_read_is_none_rather_than_an_exception():
    """The route turns None into a 404; a transport exception would become a
    500 and blame us for the printer's answer."""
    server = FakeTunnelServer()
    server.fail_next_with = 3
    host, port = await server.start()
    try:
        assert await _tunnel_to(host, port).read_bytes("/nope.3mf") is None
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_a_tunnel_delete_reports_deleted_or_failed_but_never_guesses():
    """⚠️ The tunnel's error codes have never been collected, so a refusal
    cannot be told apart from "no such file". Reporting FAILED is honest;
    reporting NOT_FOUND would be an invention that turns a 500 into a 404."""
    server = FakeTunnelServer()
    path = "/userdata/model/history/a.gcode.3mf"
    host, port = await server.start()
    try:
        assert await _tunnel_to(host, port).delete(path) is DeleteResult.DELETED
        assert server.deleted == [path]

        server.fail_next_with = 9
        assert await _tunnel_to(host, port).delete(path) is DeleteResult.FAILED
    finally:
        await server.stop()
