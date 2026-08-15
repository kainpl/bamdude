"""Finding a printer's recordings on whichever medium holds them.

⚠️ This module exists because there were three copies of the same directory
walk. The tests pin the behaviours that made the copies dangerous: falling
through to internal storage, deciding the medium from the path, and the exact
match the tunnel offers and FTP cannot.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services.timelapse_files import (
    INTERNAL_TIMELAPSE_ROOT,
    archive_subtask_name,
    list_timelapse_videos,
    match_by_model_name,
    pick_new_recording,
    read_timelapse_video,
    remove_recording_after_attach,
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
        # ⚠️ answered=True: these fixtures model a printer that REPLIES, whose
        # directories happen to be empty. An unreachable one answers False and
        # ends the card leg after a single call — pinned separately in
        # TestFtpThatStopsAnswering.
        return list(ftp or []), True

    monkeypatch.setattr("backend.app.services.bambu_ftp.list_files_checked_async", _ftp_listing)
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


class TestPickingThisPrintsRecording:
    """Two signals, and the weaker one is still the gate.

    The printer reports the absolute path of the file it last closed
    (``device.cam.timelapse_path``), which names the recording outright instead
    of assuming exactly one new file appeared. But it is the LAST recording,
    not necessarily THIS print's — two prints in quick succession, or somebody
    recording from the printer's own screen, break that. So the name is taken
    only when the baseline already agrees the file is new.
    """

    def _files(self, *names):
        return [{"name": n, "path": f"/timelapse/{n}"} for n in names]

    def test_the_named_file_wins_among_several_new_ones(self):
        picked = pick_new_recording(
            self._files("old.mp4", "a.mp4", "b.mp4"),
            {"old.mp4"},
            "/userdata/media/timelapse/b.mp4",
        )
        assert picked["name"] == "b.mp4"

    def test_without_a_report_the_old_answer_stands(self):
        picked = pick_new_recording(self._files("old.mp4", "a.mp4"), {"old.mp4"}, "")
        assert picked["name"] == "a.mp4"

    def test_a_reported_file_that_is_not_new_cannot_be_promoted(self):
        """⚠️ The guard that makes the report safe to use at all.

        A recording made from the printer's screen between our baseline and now
        is the printer's honest "last file" and somebody else's video.
        """
        picked = pick_new_recording(self._files("old.mp4", "a.mp4"), {"old.mp4"}, "/userdata/media/timelapse/old.mp4")
        assert picked["name"] == "a.mp4"

    def test_a_report_naming_a_file_we_cannot_see_falls_back(self):
        picked = pick_new_recording(self._files("old.mp4", "a.mp4"), {"old.mp4"}, "/media/usb0/timelapse/gone.mp4")
        assert picked["name"] == "a.mp4"

    def test_nothing_new_is_nothing_picked(self):
        assert pick_new_recording(self._files("old.mp4"), {"old.mp4"}, "") is None

    def test_the_medium_in_the_path_does_not_matter(self):
        """The catalogue entry carries its own path; this only matches names.
        Comparing the whole path would miss on every card recording, whose
        listing path is the FTP directory and not ``/media/usb0/…``."""
        picked = pick_new_recording(self._files("old.mp4", "a.mp4"), {"old.mp4"}, "/media/usb0/timelapse/A.MP4")
        assert picked["name"] == "a.mp4"


class TestRemovingTheRecordingFromThePrinter:
    """Opt-in tidy-up after a recording has been attached to its archive.

    ⚠️ The medium comes from the PATH, exactly as it does for the read. An FTP
    delete aimed at an internal path succeeds at doing nothing — the same shape
    of bug that once let the auto-scan find a recording it could not fetch.
    """

    @pytest.mark.asyncio
    async def test_it_does_nothing_while_the_setting_is_off(self):
        printer = SimpleNamespace(id=1, name="X2D", model="X2D")
        with (
            patch("backend.app.api.routes.settings.get_setting", new=AsyncMock(return_value=None)),
            patch("backend.app.services.printer_files.factory.transport_for") as factory,
        ):
            removed = await remove_recording_after_attach(printer, "/timelapse/video.mp4")

        assert removed is False
        factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_internal_path_is_deleted_over_the_tunnel(self):
        printer = SimpleNamespace(id=1, name="X2D", model="X2D")
        transport = SimpleNamespace(delete=AsyncMock())
        with (
            patch("backend.app.api.routes.settings.get_setting", new=AsyncMock(return_value="true")),
            patch("backend.app.services.printer_files.factory.transport_for", return_value=transport) as factory,
        ):
            removed = await remove_recording_after_attach(
                printer, "/userdata/media/timelapse/video_2026-08-14_20-53-42_4.mp4"
            )

        assert removed is True
        assert factory.call_args[0][1] == "internal"
        transport.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_card_path_is_deleted_over_ftp(self):
        printer = SimpleNamespace(id=1, name="X2D", model="X2D")
        transport = SimpleNamespace(delete=AsyncMock())
        with (
            patch("backend.app.api.routes.settings.get_setting", new=AsyncMock(return_value="true")),
            patch("backend.app.services.printer_files.factory.transport_for", return_value=transport) as factory,
        ):
            await remove_recording_after_attach(printer, "/timelapse/video.mp4")

        assert factory.call_args[0][1] == "external"

    @pytest.mark.asyncio
    async def test_a_printer_that_refuses_costs_nobody_their_archive(self):
        """The archive is already written by the time this runs. Raising here
        would abandon a completed print over a housekeeping failure."""
        printer = SimpleNamespace(id=1, name="X2D", model="X2D")
        transport = SimpleNamespace(delete=AsyncMock(side_effect=OSError("connection reset")))
        with (
            patch("backend.app.api.routes.settings.get_setting", new=AsyncMock(return_value="true")),
            patch("backend.app.services.printer_files.factory.transport_for", return_value=transport),
        ):
            assert await remove_recording_after_attach(printer, "/timelapse/video.mp4") is False


class TestFtpThatStopsAnswering:
    """A wedged FTP daemon must cost one timeout, not four.

    Observed on a live X2D: the printer's FTP stopped completing its TLS
    handshake and stayed that way until it was restarted. The recording was in
    internal storage the whole time, but the walk asked the card for four
    directories first — 30 s each — so the attach was five minutes late and the
    completed print looked stuck.

    The listing alone could not tell anyone: it returns an empty list both for
    "this directory holds nothing" and for "the printer never replied".
    """

    @pytest.mark.asyncio
    async def test_one_unanswered_directory_ends_the_whole_card_leg(self):
        printer = SimpleNamespace(id=1, name="X2D", model="X2D", ip_address="1.2.3.4", access_code="x")
        asked: list[str] = []

        async def _never_answers(ip, code, path, printer_model=None):
            asked.append(path)
            return [], False

        with (
            patch("backend.app.services.bambu_ftp.list_files_checked_async", new=_never_answers),
            patch("backend.app.services.timelapse_files._supports_internal_timelapse", return_value=False),
        ):
            videos, source = await list_timelapse_videos(printer)

        assert videos == []
        assert source is None
        assert len(asked) == 1, f"kept walking a printer that is not replying: {asked}"

    @pytest.mark.asyncio
    async def test_an_empty_directory_is_not_a_dead_printer(self):
        """The distinction the pair exists for. An empty /timelapse on a healthy
        machine must still fall through to /record, where older P1 firmware
        keeps them."""
        printer = SimpleNamespace(id=1, name="P1S", model="P1S", ip_address="1.2.3.4", access_code="x")
        asked: list[str] = []

        async def _answers(ip, code, path, printer_model=None):
            asked.append(path)
            if path == "/record":
                return [{"name": "clip.mp4", "is_directory": False, "path": "/record/clip.mp4"}], True
            return [], True

        with patch("backend.app.services.bambu_ftp.list_files_checked_async", new=_answers):
            videos, source = await list_timelapse_videos(printer)

        assert [v["name"] for v in videos] == ["clip.mp4"]
        assert source == "/record"
        assert len(asked) == 3  # /timelapse, /timelapse/video, /record

    @pytest.mark.asyncio
    async def test_a_dead_card_still_reaches_internal_storage(self):
        """The case that actually bit: the recording was internal all along."""
        printer = SimpleNamespace(id=1, name="X2D", model="X2D", ip_address="1.2.3.4", access_code="x")
        entry = SimpleNamespace(
            as_dict=lambda: {
                "name": "video_1.mp4",
                "is_directory": False,
                "path": "/userdata/media/timelapse/video_1.mp4",
            }
        )
        transport = SimpleNamespace(list_files=AsyncMock(return_value=[entry]))

        async def _never_answers(ip, code, path, printer_model=None):
            return [], False

        with (
            patch("backend.app.services.bambu_ftp.list_files_checked_async", new=_never_answers),
            patch("backend.app.services.timelapse_files._supports_internal_timelapse", return_value=True),
            patch("backend.app.services.printer_files.factory.transport_for", return_value=transport),
        ):
            videos, source = await list_timelapse_videos(printer)

        assert source == "internal"
        assert [v["name"] for v in videos] == ["video_1.mp4"]
