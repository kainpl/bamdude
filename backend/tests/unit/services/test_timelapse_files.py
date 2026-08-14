"""Finding a printer's recordings on whichever medium holds them.

⚠️ This module exists because there were three copies of the same directory
walk. The tests pin the behaviours that made the copies dangerous: falling
through to internal storage, deciding the medium from the path, and the exact
match the tunnel offers and FTP cannot.
"""

import asyncio

import pytest

from backend.app.services.timelapse_files import (
    INTERNAL_TIMELAPSE_ROOT,
    archive_subtask_name,
    list_timelapse_videos,
    match_by_model_name,
    read_timelapse_video,
)
from backend.tests.tunnel_fixtures import FakeTunnelServer


class _Printer:
    id = 1
    name = "X2D"
    ip_address = "127.0.0.1"
    access_code = "12345678"
    model = "X2D"


def _entry(name: str, model_name: str = "", *, root: str = INTERNAL_TIMELAPSE_ROOT) -> dict:
    return {
        "duration_ms": 5625,
        "model_name": model_name,
        "name": name,
        "path": f"{root}{name}",
        "size": 2253939,
        "time": 1786703523,
    }


def _patch(monkeypatch, host: str, port: int, *, ftp: list[dict] | None = None, internal: bool = True):
    from backend.app.services.printer_files.tunnel import TunnelTransport

    async def _ftp_listing(*_args, **_kwargs):
        return list(ftp or [])

    monkeypatch.setattr("backend.app.services.bambu_ftp.list_files_async", _ftp_listing)
    monkeypatch.setattr(
        "backend.app.services.printer_manager.printer_manager.get_status",
        lambda _pid: type("S", (), {"print_option_support": {"internal_timelapse": internal}})(),
    )

    def _factory(printer, _storage):
        async def connector():
            return await asyncio.open_connection(host, port)

        return TunnelTransport(printer, port=port, connector=connector)

    monkeypatch.setattr("backend.app.services.printer_files.factory.transport_for", _factory)


@pytest.mark.asyncio
async def test_the_card_is_asked_first_and_wins_when_it_has_recordings(monkeypatch):
    """A machine with both keeps today's behaviour: the card answers."""
    server = FakeTunnelServer()
    server.files = [_entry("internal.mp4", "Untitled")]
    host, port = await server.start()
    try:
        on_card = [{"name": "card.mp4", "path": "/timelapse/card.mp4", "is_directory": False, "size": 5}]
        _patch(monkeypatch, host, port, ftp=on_card)

        videos, source = await list_timelapse_videos(_Printer())
        assert [v["name"] for v in videos] == ["card.mp4"]
        assert source == "/timelapse"
        assert server.requests == []  # the tunnel was never asked
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_an_empty_card_falls_through_to_internal_storage(monkeypatch):
    """The gap this module was written for: FTP sees nothing on a cardless
    machine while the recording sits in internal storage."""
    server = FakeTunnelServer()
    server.files = [_entry("video_2026-08-14.mp4", "Untitled")]
    host, port = await server.start()
    try:
        _patch(monkeypatch, host, port, ftp=[])

        videos, source = await list_timelapse_videos(_Printer())
        assert [v["name"] for v in videos] == ["video_2026-08-14.mp4"]
        assert source == "internal"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_a_printer_that_records_only_to_a_card_is_not_probed(monkeypatch):
    """⚠️ Gated on `fun` bit 28, which is NOT the bit that gates the model
    catalogue — a machine can have one and not the other."""
    server = FakeTunnelServer()
    server.files = [_entry("internal.mp4", "Untitled")]
    host, port = await server.start()
    try:
        _patch(monkeypatch, host, port, ftp=[], internal=False)

        videos, source = await list_timelapse_videos(_Printer())
        assert videos == []
        assert source is None
        assert server.requests == []
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_non_video_entries_are_ignored_on_both_media(monkeypatch):
    server = FakeTunnelServer()
    server.files = [_entry("notes.txt"), _entry("clip.avi", "Untitled")]
    host, port = await server.start()
    try:
        _patch(monkeypatch, host, port, ftp=[])
        videos, _ = await list_timelapse_videos(_Printer())
        assert [v["name"] for v in videos] == ["clip.avi"]
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_the_path_decides_the_medium_not_the_printer_state(monkeypatch):
    """⚠️ A card inserted between the listing and the download must not turn an
    internal path into an FTP one — the file is where the listing said."""
    server = FakeTunnelServer()
    name = "video_2026-08-14.mp4"
    path = f"{INTERNAL_TIMELAPSE_ROOT}{name}"
    server.files = [_entry(name, "Untitled")]
    server.file_bytes[path] = b"\x00\x00\x00\x18ftypmp42"
    host, port = await server.start()
    try:
        _patch(monkeypatch, host, port, ftp=[])
        assert await read_timelapse_video(_Printer(), path) == b"\x00\x00\x00\x18ftypmp42"
    finally:
        await server.stop()


def test_the_model_name_match_is_an_identity_not_a_guess():
    videos = [_entry("a.mp4", "Something Else"), _entry("b.mp4", "Untitled")]
    assert match_by_model_name(videos, "Untitled")["name"] == "b.mp4"
    assert match_by_model_name(videos, "  untitled  ")["name"] == "b.mp4"
    assert match_by_model_name(videos, "Nothing Like It") is None


def test_the_match_returns_nothing_when_the_medium_has_no_such_field():
    """FTP listings carry no model_name — the callers must fall through to
    their filename and timestamp strategies rather than match on emptiness."""
    ftp_like = [{"name": "a.mp4", "path": "/timelapse/a.mp4", "is_directory": False, "size": 5}]
    assert match_by_model_name(ftp_like, "Untitled") is None
    assert match_by_model_name(ftp_like, "") is None
    assert match_by_model_name(ftp_like, None) is None


def test_several_candidate_names_are_accepted():
    """The name lives in two places and they can disagree."""
    videos = [_entry("b.mp4", "Untitled")]
    assert match_by_model_name(videos, None, "Untitled")["name"] == "b.mp4"
    assert match_by_model_name(videos, "wrong", "Untitled")["name"] == "b.mp4"


def test_the_subtask_name_is_read_from_where_the_archive_keeps_it():
    live = type("A", (), {"extra_data": {"_print_data": {"subtask_name": "From push"}}})()
    recovered = type("A", (), {"extra_data": {"original_subtask": "From meta"}})()
    empty = type("A", (), {"extra_data": None})()

    assert archive_subtask_name(live) == "From push"
    assert archive_subtask_name(recovered) == "From meta"
    assert archive_subtask_name(empty) is None
