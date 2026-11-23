"""Automatic timelapse scans stop knocking on a card that refuses to connect (upstream 36991dadc, G10 A)."""

from __future__ import annotations

import ftplib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.app.services import bambu_ftp, timelapse_files
from backend.app.services.bambu_ftp import LISTING_COMPLETE, LISTING_SLOW, LISTING_UNAVAILABLE, DirectoryListing


@pytest.fixture(autouse=True)
def _clean():
    timelapse_files._backoff.clear()
    timelapse_files._listing_answered.clear()
    yield
    timelapse_files._backoff.clear()
    timelapse_files._listing_answered.clear()


def _printer():
    return SimpleNamespace(id=9, ip_address="10.0.0.9", access_code="12345678", model="X1C", name="P")


@pytest.fixture
def card(monkeypatch):
    state = SimpleNamespace(calls=0, answered=False, refused=True)

    async def listing(ip, code, directory, **kwargs):
        state.calls += 1
        return DirectoryListing(
            directory,
            LISTING_COMPLETE if state.answered else LISTING_UNAVAILABLE,
            reason="timeout" if state.refused else None,
        )

    monkeypatch.setattr("backend.app.services.bambu_ftp.list_directory_async", listing)
    monkeypatch.setattr(timelapse_files, "_supports_internal_timelapse", lambda printer: False)
    return state


@pytest.mark.asyncio
async def test_an_automatic_scan_skips_a_card_that_refused_its_connect(card):
    await timelapse_files.list_timelapse_videos(_printer(), automatic=True)
    assert card.calls == 1
    await timelapse_files.list_timelapse_videos(_printer(), automatic=True)
    assert card.calls == 1  # held back: no socket for the same answer
    assert timelapse_files.last_listing_answered(9) is False


@pytest.mark.asyncio
async def test_a_manual_scan_is_never_held_back_and_an_answer_starts_over(card):
    await timelapse_files.list_timelapse_videos(_printer(), automatic=True)
    card.answered, card.refused = True, False
    await timelapse_files.list_timelapse_videos(_printer())  # the operator asked
    assert card.calls == 1 + len(timelapse_files.FTP_TIMELAPSE_DIRS)
    assert timelapse_files.last_listing_answered(9) is True
    await timelapse_files.list_timelapse_videos(_printer(), automatic=True)
    assert card.calls == 1 + 2 * len(timelapse_files.FTP_TIMELAPSE_DIRS)


@pytest.mark.asyncio
async def test_the_internal_catalogue_is_still_read_while_the_card_is_held_back(card, monkeypatch):
    entries = [SimpleNamespace(as_dict=lambda: {"name": "clip.mp4", "path": "/userdata/media/timelapse/clip.mp4"})]

    class Transport:
        async def list_files(self, path, file_type="model"):
            return entries

    monkeypatch.setattr(timelapse_files, "_supports_internal_timelapse", lambda printer: True)
    monkeypatch.setattr(
        "backend.app.services.printer_files.factory.transport_for", lambda printer, storage: Transport()
    )
    await timelapse_files.list_timelapse_videos(_printer(), automatic=True)
    videos, source = await timelapse_files.list_timelapse_videos(_printer(), automatic=True)
    assert source == "internal"
    assert [v["name"] for v in videos] == ["clip.mp4"]


def test_the_backoff_is_bounded():
    printer = _printer()
    for _ in range(12):
        timelapse_files._note_unreachable(printer)
    _until, step = timelapse_files._backoff[timelapse_files._endpoint(printer)]
    assert step == timelapse_files._BACKOFF_MAX


@pytest.mark.asyncio
async def test_list_timeout_backs_off_only_automatic_scans(monkeypatch):
    calls = []
    outcome = LISTING_SLOW

    async def listing(ip, code, directory, **kwargs):
        calls.append(directory)
        return DirectoryListing(directory, outcome, reason="timeout" if outcome == LISTING_SLOW else None)

    monkeypatch.setattr("backend.app.services.bambu_ftp.list_directory_async", listing)
    monkeypatch.setattr(timelapse_files, "_supports_internal_timelapse", lambda _: False)
    printer = _printer()
    await timelapse_files.list_timelapse_videos(printer, automatic=True)
    await timelapse_files.list_timelapse_videos(printer, automatic=True)
    assert calls == ["/timelapse"]
    assert not timelapse_files.last_listing_answered(printer.id)
    outcome = LISTING_COMPLETE
    await timelapse_files.list_timelapse_videos(printer)
    assert timelapse_files.last_listing_answered(printer.id)
    await timelapse_files.list_timelapse_videos(printer, automatic=True)
    assert len(calls) == 1 + 2 * len(timelapse_files.FTP_TIMELAPSE_DIRS)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [TimeoutError("LIST stalled"), ftplib.error_temp("421 Too many connections")], ids=["timeout", "421"]
)
async def test_550_parent_list_failure_backs_off_and_manual_answer_clears_it(monkeypatch, failure):
    healthy = False
    control = MagicMock()

    def cwd(path):
        if path == "/timelapse" and not healthy:
            raise ftplib.error_perm("550 path unavailable")

    def list_entries(*_):
        if not healthy:
            raise failure

    control.cwd.side_effect = cwd
    control.retrlines.side_effect = list_entries
    factory = MagicMock(return_value=control)
    monkeypatch.setattr(bambu_ftp, "ImplicitFTP_TLS", factory)
    monkeypatch.setattr(timelapse_files, "_supports_internal_timelapse", lambda _: False)
    printer = _printer()

    for _ in range(2):
        assert await timelapse_files.list_timelapse_videos(printer, automatic=True) == ([], None)
        assert not timelapse_files.last_listing_answered(printer.id)
    factory.assert_called_once()
    assert [call.args[0] for call in control.cwd.call_args_list] == ["/timelapse", "/"]

    healthy = True
    assert await timelapse_files.list_timelapse_videos(printer) == ([], None)
    assert timelapse_files.last_listing_answered(printer.id)
    await timelapse_files.list_timelapse_videos(printer, automatic=True)
    assert factory.call_count == 1 + 2 * len(timelapse_files.FTP_TIMELAPSE_DIRS)
