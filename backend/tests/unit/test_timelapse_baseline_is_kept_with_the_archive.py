"""The boundary the timelapse scan diffs against is a fact of the archive (G10 C3, M10/M11)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services import timelapse_baseline as tb


def _v(name: str) -> dict:
    return {"name": name, "is_directory": False, "size": 1, "path": f"/timelapse/{name}"}


class TestTheStoredRecord:
    def test_it_round_trips(self):
        stored = tb.record({"b.mp4", "a.mp4"}, trusted=True, taken=tb.TAKEN_AT_START)
        assert stored["names"] == ["a.mp4", "b.mp4"]
        assert tb.read({tb.KEY: stored}) == ({"a.mp4", "b.mp4"}, True)

    def test_an_unread_card_is_remembered_as_unread(self):
        stored = tb.record(set(), trusted=False, taken=tb.TAKEN_LATE)
        assert tb.read({tb.KEY: stored}) == (set(), False)

    @pytest.mark.parametrize(
        "extra",
        [
            None,
            {},
            {tb.KEY: "yes"},
            {tb.KEY: {"names": "a.mp4", "trusted": True, "taken": "start"}},
            {tb.KEY: {"names": [1, 2], "trusted": True, "taken": "start"}},
            {tb.KEY: {"names": [], "trusted": "yes", "taken": "start"}},
            {tb.KEY: {"names": [], "trusted": True, "taken": "after the print"}},
            {tb.KEY: {"names": [], "trusted": True}},
        ],
    )
    def test_anything_malformed_is_no_baseline(self, extra):
        assert tb.read(extra) is None


class TestWhenTheScanMayAttachByItself:
    def test_a_trusted_boundary_allows_it(self):
        assert tb.automatic_attach_refusal({"old.mp4"}, True) is None
        assert tb.automatic_attach_refusal(set(), True) is None  # an empty card that ANSWERED is a boundary

    def test_no_boundary_refuses(self):
        assert tb.automatic_attach_refusal(None, True) == tb.NO_BOUNDARY

    def test_an_unread_boundary_refuses_even_when_it_lists_nothing(self):
        assert tb.automatic_attach_refusal(set(), False) == tb.UNREAD_BOUNDARY


class TestSaving:
    async def test_it_lands_in_the_archive_and_keeps_what_was_there(self, db_session, archive_factory, printer_factory):
        from backend.app.models.archive import PrintArchive

        archive = await archive_factory((await printer_factory()).id, file_path="", extra_data={"other": 1})
        archive_id, identity = archive.id, archive.recovery_identity

        assert await tb.save(db_session, archive_id, identity, tb.record({"a.mp4"}, trusted=True, taken="start"))

        db_session.expire_all()
        extra = (await db_session.get(PrintArchive, archive_id)).extra_data
        assert extra["other"] == 1
        assert tb.read(extra) == ({"a.mp4"}, True)

    async def test_a_reused_archive_id_is_not_given_the_baseline(self, db_session, archive_factory, printer_factory):
        from backend.app.models.archive import PrintArchive

        archive = await archive_factory((await printer_factory()).id, file_path="")
        archive_id = archive.id
        assert not await tb.save(
            db_session, archive_id, "an-earlier-archive-of-this-id", tb.record(set(), trusted=True, taken="start")
        )
        db_session.expire_all()
        assert tb.read((await db_session.get(PrintArchive, archive_id)).extra_data) is None


def _scan_env(videos, *, extra_data=None):
    archive = MagicMock()
    archive.id = 1
    archive.timelapse_path = None
    archive.printer_id = 1
    archive.filename = "benchy.gcode.3mf"
    archive.recovery_identity = "ID-1"
    archive.extra_data = extra_data
    printer = SimpleNamespace(id=1, name="P", model="P1S", connector_identity="P-1")
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=printer)))
    service = MagicMock()
    service.get_archive = AsyncMock(return_value=archive)
    service.attach_timelapse = AsyncMock(return_value=True)
    listing = AsyncMock(return_value=(videos, "/timelapse"))
    return session, service, listing


async def _scan(videos, *, baseline=None, extra_data=None, may_run=None, trusted=True):
    from backend.app import main

    session, service, listing = _scan_env(videos, extra_data=extra_data)
    with (
        patch("backend.app.main.async_session", return_value=session),
        patch("backend.app.main._list_timelapse_videos", new=listing),
        patch("backend.app.main.ws_manager", MagicMock(send_archive_updated=AsyncMock())),
        patch("backend.app.main.asyncio.sleep", new_callable=AsyncMock),
        patch("backend.app.main.ArchiveService", return_value=service),
        patch("backend.app.main.read_timelapse_video", new=AsyncMock(return_value=b"video")) as read,
        patch("backend.app.main.remove_recording_after_attach", new=AsyncMock()) as remove,
    ):
        await main._scan_for_timelapse_with_retries(1, baseline, baseline_trusted=trusted, may_run=may_run)
    return SimpleNamespace(service=service, listing=listing, read=read, remove=remove)


class TestTheScanAfterARestart:
    """M10: memory lost the baseline; the archive did not."""

    async def test_the_archives_baseline_is_used(self):
        kept = {tb.KEY: tb.record({"old.mp4"}, trusted=True, taken=tb.TAKEN_AT_START)}
        ran = await _scan([_v("old.mp4"), _v("ours.mp4")], extra_data=kept)
        ran.service.attach_timelapse.assert_awaited_once()
        assert ran.service.attach_timelapse.await_args.args[2] == "ours.mp4"

    async def test_an_unread_baseline_stays_unread_across_the_restart(self):
        """The restart does not turn "the card did not answer" into "the card was empty"."""
        kept = {tb.KEY: tb.record(set(), trusted=False, taken=tb.TAKEN_AT_START)}
        ran = await _scan([_v("old.mp4")], extra_data=kept)
        ran.service.attach_timelapse.assert_not_called()
        ran.read.assert_not_called()
        ran.remove.assert_not_called()

    async def test_without_any_baseline_one_old_video_is_not_attached(self):
        """The old fallback listed the card now and found "nothing new" at best — or, unread, everything new."""
        ran = await _scan([_v("old.mp4")], extra_data={})
        ran.service.attach_timelapse.assert_not_called()
        ran.listing.assert_not_called()  # not even a listing: there is nothing to compare it with
        ran.read.assert_not_called()

    async def test_a_late_baseline_makes_what_existed_then_not_new(self):
        kept = {tb.KEY: tb.record({"old.mp4", "ours.mp4"}, trusted=True, taken=tb.TAKEN_LATE)}
        ran = await _scan([_v("old.mp4"), _v("ours.mp4")], extra_data=kept)
        ran.service.attach_timelapse.assert_not_called()

    async def test_the_baseline_in_memory_outranks_the_stored_one(self):
        kept = {tb.KEY: tb.record(set(), trusted=False, taken=tb.TAKEN_LATE)}
        ran = await _scan([_v("old.mp4"), _v("ours.mp4")], baseline={"old.mp4"}, extra_data=kept)
        ran.service.attach_timelapse.assert_awaited_once()


class TestANewerPrintStopsTheScan:
    """M11: before a listing, before a download, before the attach."""

    async def test_before_the_listing(self):
        ran = await _scan([_v("ours.mp4")], baseline=set(), may_run=lambda: False)
        ran.listing.assert_not_called()
        ran.service.attach_timelapse.assert_not_called()

    async def test_before_the_download(self):
        answers = iter([True, False])  # the listing was allowed; then B started
        ran = await _scan([_v("ours.mp4")], baseline=set(), may_run=lambda: next(answers, False))
        ran.listing.assert_awaited_once()
        ran.read.assert_not_called()
        ran.service.attach_timelapse.assert_not_called()

    async def test_before_the_attach_and_nothing_is_deleted_from_the_printer(self):
        answers = iter([True, True, False])  # listed, picked and downloaded; then B started
        ran = await _scan([_v("ours.mp4")], baseline=set(), may_run=lambda: next(answers, False))
        ran.read.assert_awaited_once()
        ran.service.attach_timelapse.assert_not_called()
        ran.remove.assert_not_called()

    async def test_the_same_run_goes_through(self):
        ran = await _scan([_v("ours.mp4")], baseline=set(), may_run=lambda: True)
        ran.service.attach_timelapse.assert_awaited_once()
        ran.remove.assert_awaited_once()


class TestTheAttachNamesItsSource:
    async def test_the_recording_is_claimed_under_the_printers_identity(self):
        ran = await _scan([_v("ours.mp4")], baseline=set())
        kwargs = ran.service.attach_timelapse.await_args.kwargs
        assert kwargs["origin"] == "printer"
        assert (kwargs["remote"].printer_identity, kwargs["remote"].storage, kwargs["remote"].path) == (
            "P-1",
            "external",
            "/timelapse/ours.mp4",
        )
