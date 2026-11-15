"""Automatic timelapse scans stop knocking on a card that refuses to connect (upstream 36991dadc, G10 A)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.app.services import timelapse_files


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

    async def listing(ip, code, directory, printer_model=None):
        state.calls += 1
        return [], state.answered

    monkeypatch.setattr("backend.app.services.bambu_ftp.list_files_checked_async", listing)
    monkeypatch.setattr(
        "backend.app.services.bambu_ftp.BambuFTPClient.connect_failure_since",
        classmethod(lambda cls, ip, since: "timeout" if state.refused else None),
    )
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
