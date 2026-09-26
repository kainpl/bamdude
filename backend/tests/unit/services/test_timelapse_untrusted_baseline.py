"""A timelapse baseline taken off a card that did not answer is not evidence (audit D6 part 4, upstream 59d2713a).

The completion scan attaches the recording that is NEW since the print started,
and the print-start baseline is what "new" is measured against. When the card
did not answer at that moment, the baseline records "nothing there" for a card
nobody read — every video on it then counts as new, the first one wins, and a
stale recording is attached to this print (and, with the tidy-up on, deleted
off the printer).

The empty baseline is still recorded, as upstream argues: the usual card holds
one recording at completion, and an empty baseline resolves it. What changes is
that the scan no longer GUESSES between several new ones — it leaves them to
Scan for timelapse. Two kinds of evidence still stand on their own: a single
new recording, and the file the printer itself names as the one it just closed.

The signal differs from upstream's — we have no FTPS cool-off. Our listing
already knows whether the printer answered (``list_files_checked_async``) and
used to discard it; it now keeps it per printer.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services import timelapse_files
from backend.app.services.timelapse_files import last_listing_answered, list_timelapse_videos, pick_new_recording


class _Printer:
    id = 5
    name = "P1S"
    ip_address = "127.0.0.1"
    access_code = "12345678"
    model = "P1S"


@pytest.fixture(autouse=True)
def _fresh():
    from backend.app import main

    timelapse_files._listing_answered.clear()
    main._timelapse_baselines.clear()
    main._untrusted_timelapse_baselines.clear()
    yield
    timelapse_files._listing_answered.clear()
    main._timelapse_baselines.clear()
    main._untrusted_timelapse_baselines.clear()


def _card(monkeypatch, *, answered: bool, files: list[dict] | None = None, internal: bool = False):
    async def listing(*_args, **_kwargs):
        return (list(files or []), answered)

    monkeypatch.setattr("backend.app.services.bambu_ftp.list_files_checked_async", listing)
    monkeypatch.setattr(
        "backend.app.services.printer_manager.printer_manager.get_status",
        lambda _pid: type("S", (), {"print_option_support": {"internal_timelapse": internal}})(),
    )


# -- the listing remembers whether it heard from the printer -------------------


@pytest.mark.asyncio
async def test_a_card_that_does_not_answer_is_not_an_empty_card(monkeypatch):
    _card(monkeypatch, answered=False)
    assert await list_timelapse_videos(_Printer()) == ([], None)
    assert last_listing_answered(_Printer.id) is False


@pytest.mark.asyncio
async def test_an_empty_card_that_answered_is_believed(monkeypatch):
    _card(monkeypatch, answered=True)
    assert await list_timelapse_videos(_Printer()) == ([], None)
    assert last_listing_answered(_Printer.id) is True


@pytest.mark.asyncio
async def test_an_internal_catalogue_that_fails_is_not_believed(monkeypatch):
    _card(monkeypatch, answered=True, internal=True)

    class _Broken:
        async def list_files(self, *_args, **_kwargs):
            raise OSError("tunnel refused")

    monkeypatch.setattr("backend.app.services.printer_files.factory.transport_for", lambda *_a: _Broken())
    assert await list_timelapse_videos(_Printer()) == ([], None)
    assert last_listing_answered(_Printer.id) is False


@pytest.mark.asyncio
async def test_a_later_answer_replaces_an_earlier_silence(monkeypatch):
    _card(monkeypatch, answered=False)
    await list_timelapse_videos(_Printer())
    video = {"name": "a.mp4", "path": "/timelapse/a.mp4", "is_directory": False}
    _card(monkeypatch, answered=True, files=[video])
    await list_timelapse_videos(_Printer())
    assert last_listing_answered(_Printer.id) is True


def test_a_printer_never_listed_counts_as_answered():
    assert last_listing_answered(999) is True


# -- the picker will not guess -------------------------------------------------


def _v(name: str) -> dict:
    return {"name": name, "path": f"/timelapse/{name}"}


def test_one_new_recording_still_resolves_without_a_trusted_baseline():
    assert pick_new_recording([_v("a.mp4")], set(), "", require_unambiguous=True) == _v("a.mp4")


def test_several_new_recordings_are_not_guessed_between():
    assert pick_new_recording([_v("a.mp4"), _v("b.mp4")], set(), "", require_unambiguous=True) is None


def test_the_file_the_printer_names_still_answers():
    picked = pick_new_recording(
        [_v("a.mp4"), _v("b.mp4")], set(), "/media/usb0/timelapse/b.mp4", require_unambiguous=True
    )
    assert picked == _v("b.mp4")


def test_a_trusted_baseline_keeps_taking_the_first_new_one():
    assert pick_new_recording([_v("a.mp4"), _v("b.mp4")], set(), "") == _v("a.mp4")


# -- the baseline is marked, and the scan honours the mark ---------------------


@pytest.mark.asyncio
async def test_a_baseline_off_a_silent_card_is_marked_untrusted():
    from backend.app import main

    async def silent(printer):
        timelapse_files._listing_answered[printer.id] = False
        return [], None

    with patch("backend.app.main._list_timelapse_videos", side_effect=silent):
        await main._capture_timelapse_baseline_at_start(_Printer(), _Printer.id, MagicMock())

    assert main._timelapse_baselines[_Printer.id] == set(), "still recorded — one video resolves"
    assert _Printer.id in main._untrusted_timelapse_baselines


@pytest.mark.asyncio
async def test_a_baseline_off_an_answering_card_is_trusted():
    from backend.app import main

    main._untrusted_timelapse_baselines.add(_Printer.id)  # left over from the last print

    async def answered(printer):
        timelapse_files._listing_answered[printer.id] = True
        return [_v("old.mp4")], "/timelapse"

    with patch("backend.app.main._list_timelapse_videos", side_effect=answered):
        await main._capture_timelapse_baseline_at_start(_Printer(), _Printer.id, MagicMock())

    assert main._timelapse_baselines[_Printer.id] == {"old.mp4"}
    assert _Printer.id not in main._untrusted_timelapse_baselines


def _scan_mocks():
    archive = MagicMock(id=1, timelapse_path=None, printer_id=_Printer.id, filename="lamp.gcode.3mf")
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=_Printer())))
    service = MagicMock()
    service.get_archive = AsyncMock(return_value=archive)
    service.attach_timelapse = AsyncMock(return_value=True)
    return session, service


async def _scan(videos: list[dict], *, baseline_trusted: bool):
    from backend.app import main

    session, service = _scan_mocks()
    with (
        patch("backend.app.main.async_session", return_value=session),
        patch("backend.app.main._list_timelapse_videos", new=AsyncMock(return_value=(videos, "/timelapse"))),
        patch("backend.app.main.ws_manager", MagicMock(send_archive_updated=AsyncMock())),
        patch("backend.app.main.asyncio.sleep", new_callable=AsyncMock),
        patch("backend.app.main.ArchiveService", return_value=service),
        patch("backend.app.main.read_timelapse_video", new=AsyncMock(return_value=b"video")) as read,
        patch("backend.app.main.remove_recording_after_attach", new=AsyncMock()) as remove,
    ):
        await main._scan_for_timelapse_with_retries(1, set(), baseline_trusted=baseline_trusted)
    return service, read, remove


@pytest.mark.asyncio
async def test_the_scan_leaves_several_candidates_alone_after_an_untrusted_baseline():
    service, read, remove = await _scan([_v("stale.mp4"), _v("ours.mp4")], baseline_trusted=False)
    service.attach_timelapse.assert_not_called()
    read.assert_not_called()
    remove.assert_not_called()


@pytest.mark.asyncio
async def test_the_scan_still_takes_a_single_candidate_after_an_untrusted_baseline():
    service, _read, _remove = await _scan([_v("ours.mp4")], baseline_trusted=False)
    service.attach_timelapse.assert_awaited_once()
    assert service.attach_timelapse.await_args.args[2] == "ours.mp4"


def test_completion_takes_the_mark_with_the_baseline():
    """``on_print_complete`` hands the scan both, and neither outlives the print."""
    from backend.app import main

    main._timelapse_baselines[_Printer.id] = set()
    main._untrusted_timelapse_baselines.add(_Printer.id)

    assert main._pop_timelapse_baseline(_Printer.id) == (set(), False)
    assert _Printer.id not in main._timelapse_baselines
    assert _Printer.id not in main._untrusted_timelapse_baselines
    assert main._pop_timelapse_baseline(_Printer.id) == (None, True), "no baseline: the scan takes its own"
