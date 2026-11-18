"""An archive's chooser and its download jobs (G10 D, M19, M20).

The archive's attached video is the archive's own: it needs no printer right
and no printer. What the printer still holds is a second, separate question.
"""

from __future__ import annotations

import asyncio
import io
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.app.api.routes import media_downloads as routes
from backend.app.services import media_downloads
from backend.app.services.media_downloads import media_download_runtime
from backend.app.services.printer_files.listing import ListingAnswer
from backend.tests.integration.test_media_download_jobs import (  # noqa: F401 -- the fixture is autouse here too
    FakeTransport,
    _patient_authority,
    _until,
)
from backend.tests.integration.test_workshop_library_rights import _jwt, _key, _user

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

STARTED = datetime(2026, 11, 10, 9, 0, 0)
ENDED = datetime(2026, 11, 10, 11, 0, 0)


@pytest.fixture
async def archive_media(monkeypatch, tmp_path, async_client, printer_factory, archive_factory, db_session):
    """An archive with an attached video on disk, on a printer whose files are a fake transport."""
    from backend.app.models.archive_media_asset import ROLE_PRIMARY, ArchiveMediaAsset

    transport = FakeTransport({})
    monkeypatch.setattr(
        "backend.app.services.printer_files.factory.transport_for", lambda _printer, _storage: transport
    )
    monkeypatch.setattr(media_downloads, "_base_reserve", lambda _usage: 0)
    monkeypatch.setattr(routes.settings, "base_dir", tmp_path)
    media_download_runtime.reopen()
    media_download_runtime.root = tmp_path / "media-downloads"

    printer = await printer_factory()
    relative = "archive/1/clip/timelapse.mp4"
    (tmp_path / relative).parent.mkdir(parents=True)
    (tmp_path / relative).write_bytes(b"local-video" * 300)
    archive = await archive_factory(
        printer.id, print_name="Lamp", timelapse_path=relative, started_at=STARTED, completed_at=ENDED
    )
    asset = ArchiveMediaAsset(
        asset_uuid=str(uuid.uuid4()),
        archive_id=archive.id,
        archive_identity=archive.recovery_identity,
        role=ROLE_PRIMARY,
        revision=1,
        relative_path=relative,
        size_bytes=3300,
        origin="upload",
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db_session.add(asset)
    await db_session.commit()

    listings = SimpleNamespace(videos=[], media={"external": "answered", "internal": "not_asked"}, camera=None)

    async def list_videos(_printer, **_kwargs):
        if isinstance(listings.videos, Exception):
            raise listings.videos
        return list(listings.videos), "/timelapse"

    async def list_camera(_printer, _storage, path, _type, _transport, *, wait):
        assert path == "/ipcam"
        return listings.camera or ListingAnswer("missing")

    monkeypatch.setattr("backend.app.services.timelapse_files.list_timelapse_videos", list_videos)
    monkeypatch.setattr("backend.app.services.timelapse_files.last_listing_media", lambda _pid: dict(listings.media))
    monkeypatch.setattr("backend.app.services.printer_files.listing.listing_coalescer.list", list_camera)
    box = SimpleNamespace(
        transport=transport,
        printer=printer,
        archive=archive,
        asset=routes.local_asset_id(f"1:{asset.asset_uuid}"),
        listings=listings,
        base=f"/api/v1/archives/{archive.id}/media-download-jobs",
        sources=f"/api/v1/archives/{archive.id}/media-sources",
    )
    try:
        yield box
    finally:
        transport.worker_ended.set()
        if transport.hold is not None:
            transport.hold.set()
        assert await media_download_runtime.quiesce(5)
        media_download_runtime.reopen()
        media_download_runtime.root = None


def _video(name, mtime, *, quality=None, path=None, size=10):
    entry = {"name": name, "path": path or f"/timelapse/{name}", "size": size, "mtime": mtime}
    if quality:
        entry["mtime_quality"] = quality
    return entry


def _camera_file(name, mtime, quality="date_only"):
    from backend.app.services.printer_files.base import RemoteFile

    return RemoteFile(name, f"/ipcam/{name}", 5, False, mtime, quality)


class TestChooser:
    async def test_the_three_lists_answer_separately(self, async_client, archive_media):
        archive_media.listings.videos = [_video("video_a.mp4", ENDED)]
        archive_media.listings.camera = ListingAnswer(
            "complete",
            [
                _camera_file("ipcam-record.2026-11-10_09-30.mp4", ENDED),
                _camera_file("notes.txt", ENDED),
                _camera_file("other-video.mp4", ENDED),
            ],
        )
        answer = (await async_client.get(archive_media.sources)).json()
        assert answer["local"] == {
            "status": "ok",
            "items": [{"asset": archive_media.asset, "name": "Lamp.mp4", "size": 3300}],
        }
        assert answer["printer_id"] == archive_media.printer.id
        assert answer["timelapse"]["status"] == "ok"
        assert [i["name"] for i in answer["timelapse"]["items"]] == ["video_a.mp4"]
        # Only the camera's own chunks; no other file of the directory.
        assert [i["name"] for i in answer["camera"]["items"]] == ["ipcam-record.2026-11-10_09-30.mp4"]
        assert answer["window"]["margin_minutes"] == 10
        # No path on this server leaks into the local item.
        assert "path" not in answer["local"]["items"][0]

    async def test_an_unconfirmed_time_is_never_called_a_match(self, async_client, archive_media):
        inside, outside = ENDED, ENDED + timedelta(days=3)
        archive_media.listings.videos = [
            _video("exact_in.mp4", inside, quality="exact", path="/userdata/media/timelapse/exact_in.mp4"),
            _video("exact_out.mp4", outside, quality="exact", path="/userdata/media/timelapse/exact_out.mp4"),
            _video("wall_in.mp4", inside, quality="year_inferred"),
            _video("no_time.mp4", None),
        ]
        items = {i["name"]: i for i in (await async_client.get(archive_media.sources)).json()["timelapse"]["items"]}
        assert (items["exact_in.mp4"]["overlap"], items["exact_in.mp4"]["time_basis"]) == (True, "exact")
        assert items["exact_in.mp4"]["storage"] == "internal"
        assert items["exact_out.mp4"]["overlap"] is False
        # An FTP wall clock of unknown zone: shown, never preselected.
        assert (items["wall_in.mp4"]["overlap"], items["wall_in.mp4"]["time_basis"]) == (None, "unconfirmed")
        assert (items["no_time.mp4"]["overlap"], items["no_time.mp4"]["time_basis"]) == (None, "unknown")

    async def test_the_operators_offset_is_applied_and_marked_as_an_assumption(self, async_client, archive_media):
        # The printer's clock runs three hours ahead of UTC: its wall time is 14:00 at the print's end.
        archive_media.listings.videos = [_video("wall.mp4", ENDED + timedelta(hours=3), quality="year_inferred")]
        plain = (await async_client.get(archive_media.sources)).json()["timelapse"]["items"][0]
        assert (plain["overlap"], plain["assumed"]) == (None, False)
        shifted = (await async_client.get(archive_media.sources, params={"offset_minutes": 180})).json()
        item = shifted["timelapse"]["items"][0]
        assert (item["overlap"], item["assumed"], shifted["offset_minutes"]) == (True, True, 180)
        wrong = (await async_client.get(archive_media.sources, params={"offset_minutes": -600})).json()
        assert wrong["timelapse"]["items"][0]["overlap"] is False
        wide = (await async_client.get(archive_media.sources, params={"margin_minutes": 720})).json()
        assert wide["window"]["margin_minutes"] == 720

    async def test_a_printer_that_does_not_answer_is_not_an_empty_list(self, async_client, archive_media):
        archive_media.listings.media = {"external": "unavailable", "internal": "not_asked"}
        archive_media.listings.camera = ListingAnswer("unavailable", reason="connect")
        answer = (await async_client.get(archive_media.sources)).json()
        assert answer["timelapse"]["status"] == "unavailable" and answer["camera"]["status"] == "unavailable"
        # The local video is still offered.
        assert answer["local"]["status"] == "ok"

        archive_media.listings.media = {"external": "answered", "internal": "not_asked"}
        archive_media.listings.camera = ListingAnswer("slow", reason="in_progress")
        answer = (await async_client.get(archive_media.sources)).json()
        assert (answer["timelapse"]["status"], answer["camera"]["status"]) == ("empty", "slow")

        archive_media.listings.videos = RuntimeError("boom")
        assert (await async_client.get(archive_media.sources)).json()["timelapse"]["status"] == "unavailable"

    async def test_without_the_printer_right_the_local_video_stays(self, async_client, archive_media, db_session):
        await _user(db_session, "ch_history", ["archives:read_all"])
        answer = (await async_client.get(archive_media.sources, headers=await _jwt("ch_history"))).json()
        assert answer["local"]["status"] == "ok"
        assert (answer["timelapse"], answer["camera"]) == (
            {"status": "forbidden", "items": []},
            {"status": "forbidden", "items": []},
        )
        assert answer["printer_id"] is None

    async def test_a_key_without_the_printer_reads_the_local_video_only(
        self, async_client, archive_media, db_session, printer_factory
    ):
        other = await printer_factory(name="Other", serial_number="OTHER0002")
        owner = await _user(db_session, "ch_key", ["archives:read_all", "printers:files", "printers:read"])
        raw = await _key(db_session, owner, can_manage_archives=True, can_control_printer=True, printer_ids=[other.id])
        key = {"X-API-Key": raw, "Authorization": ""}
        answer = await async_client.get(archive_media.sources, headers=key)
        assert answer.status_code == 200, answer.text
        assert answer.json()["local"]["status"] == "ok"
        assert answer.json()["timelapse"]["status"] == "forbidden"

        # The local video downloads without the printer…
        created = await async_client.post(archive_media.base, json={"assets": [archive_media.asset]}, headers=key)
        assert created.status_code == 202, created.text
        job_id = created.json()["job_id"]
        await _until(async_client, f"{archive_media.base}/{job_id}", headers=key)
        artifact = await async_client.get(f"{archive_media.base}/{job_id}/artifact", headers=key)
        assert artifact.status_code == 200 and artifact.content == b"local-video" * 300
        # …and a recording on that printer is refused by the key's printer list.
        remote = {"printer_id": archive_media.printer.id, "storage": "external", "path": "/timelapse/a.mp4"}
        refused = await async_client.post(archive_media.base, json={"remote": [remote]}, headers=key)
        assert refused.status_code == 403
        assert archive_media.transport.calls == []

    async def test_an_archive_somebody_else_owns_is_404(self, async_client, archive_media, db_session):
        await _user(db_session, "ch_own", ["archives:read_own", "printers:files"])
        headers = await _jwt("ch_own")
        assert (await async_client.get(archive_media.sources, headers=headers)).status_code == 404
        refused = await async_client.post(archive_media.base, json={"assets": [archive_media.asset]}, headers=headers)
        assert refused.status_code == 404


class TestJobs:
    async def test_the_local_video_and_recordings_go_into_one_archive(self, async_client, archive_media):
        archive_media.transport.files.update(
            {"/timelapse/video_a.mp4": b"remote-a", "/ipcam/ipcam-record.1.mp4": b"chunk-1"}
        )
        pid = archive_media.printer.id
        body = {
            "assets": [archive_media.asset],
            "remote": [
                {"printer_id": pid, "storage": "external", "path": "/timelapse/video_a.mp4", "size": 8},
                {"printer_id": pid, "storage": "external", "path": "/ipcam/ipcam-record.1.mp4"},
            ],
        }
        created = await async_client.post(archive_media.base, json=body)
        assert created.status_code == 202, created.text
        job = created.json()
        assert (job["kind"], job["as_zip"], job["filename"]) == ("archive", True, "Lamp.zip")
        ready = await _until(async_client, f"{archive_media.base}/{job['job_id']}")
        assert (ready["phase"], ready["succeeded"]) == ("ready", 3)
        minted = await async_client.post(f"{archive_media.base}/{job['job_id']}/download-token", json={})
        answer = await async_client.get(minted.json()["url"], headers={"Authorization": ""})
        assert answer.status_code == 200
        with zipfile.ZipFile(io.BytesIO(answer.content)) as archive:
            assert archive.namelist() == ["Lamp.mp4", "video_a.mp4", "ipcam-record.1.mp4"]
            assert archive.read("Lamp.mp4") == b"local-video" * 300
        # The printer's job list is another scope: this job is not in it.
        printer_jobs = await async_client.get(f"/api/v1/printers/{pid}/files/download-jobs")
        assert printer_jobs.json() == {"jobs": []}

    async def test_a_recording_on_another_printer_is_refused(self, async_client, archive_media, printer_factory):
        other = await printer_factory(name="Other", serial_number="OTHER0003")
        remote = {"printer_id": other.id, "storage": "external", "path": "/timelapse/a.mp4"}
        refused = await async_client.post(archive_media.base, json={"remote": [remote]})
        assert refused.status_code == 400
        assert archive_media.transport.calls == []

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"assets": ["a", "a"]},
            {"remote": [{"printer_id": 1, "storage": "udisk", "path": "/a.mp4"}]},
            {"remote": [{"printer_id": 1, "storage": "external", "path": "../a.mp4"}]},
        ],
    )
    async def test_a_selection_is_validated(self, async_client, archive_media, body):
        assert (await async_client.post(archive_media.base, json=body)).status_code == 422

    async def test_a_video_that_was_replaced_is_no_longer_offered(self, async_client, archive_media):
        refused = await async_client.post(archive_media.base, json={"assets": [str(uuid.uuid4())]})
        assert refused.status_code == 404

    async def test_losing_the_printer_right_stops_a_job_that_reads_the_printer(
        self, async_client, archive_media, db_session
    ):
        from sqlalchemy import select

        from backend.app.models.group import Group
        from backend.app.services.live_authority import live_authority

        await _user(db_session, "ch_loses", ["archives:read_all", "printers:files", "printers:read"])
        headers = await _jwt("ch_loses")
        archive_media.transport.files["/timelapse/a.mp4"] = b"a"
        archive_media.transport.block()
        remote = {"printer_id": archive_media.printer.id, "storage": "external", "path": "/timelapse/a.mp4"}
        created = await async_client.post(archive_media.base, json={"remote": [remote]}, headers=headers)
        assert created.status_code == 202, created.text
        job = media_download_runtime.jobs[created.json()["job_id"]]
        await asyncio.wait_for(archive_media.transport.entered.wait(), 3)

        group = (await db_session.execute(select(Group).where(Group.name == "grp-ch_loses"))).scalar_one()
        group.permissions = ["archives:read_all"]
        await db_session.commit()
        live_authority.invalidate()
        await asyncio.wait_for(archive_media.transport.cancel_seen.wait(), 6)
        archive_media.transport.worker_ended.set()
        await asyncio.wait_for(job.done.wait(), 3)
        assert (job.phase, job.error) == ("failed", "authority_lost")


class TestWindow:
    async def test_an_open_archive_does_not_cover_everything_since(self):
        archive = SimpleNamespace(started_at=STARTED, completed_at=None)
        now = STARTED + timedelta(days=30)
        low, high = routes.archive_window(archive, timedelta(minutes=10), now)
        assert low == STARTED - timedelta(minutes=10)
        assert high == STARTED + timedelta(hours=48, minutes=10)
        assert routes.archive_window(SimpleNamespace(started_at=None, completed_at=ENDED), timedelta(0)) is None

    async def test_overlap_rules(self):
        window = (STARTED, ENDED)
        assert routes.time_overlap(ENDED, "exact", window, None) == (True, False)
        assert routes.time_overlap(ENDED, "exact", window, timedelta(hours=5)) == (True, False)  # exact ignores it
        assert routes.time_overlap(ENDED + timedelta(hours=2), "year_inferred", window, None) == (None, False)
        shifted = routes.time_overlap(ENDED + timedelta(hours=2), "year_inferred", window, timedelta(hours=2))
        assert shifted == (True, True)
        assert routes.time_overlap(None, "exact", window, None) == (None, False)
        assert routes.time_overlap(ENDED, "exact", None, None) == (None, False)
        aware = ENDED.replace(tzinfo=timezone.utc)
        assert routes.time_overlap(aware, "exact", window, None) == (True, False)

    async def test_a_day_without_an_hour_can_only_rule_a_file_out(self):
        # Codex r1 on decision 10: an offset does not tell which hour of the day a file was written.
        window = (STARTED, ENDED)  # 10 November, 09:00–11:00
        day = datetime(2026, 11, 10)
        assert routes.time_overlap(day, "date_only", window, None) == (None, False)
        # The day touches the window: still unknown, never a match.
        assert routes.time_overlap(day, "date_only", window, timedelta(0)) == (None, False)
        assert routes.time_overlap(datetime(2026, 11, 8), "date_only", window, timedelta(0)) == (False, True)
        assert routes.time_overlap(datetime(2026, 11, 12), "date_only", window, timedelta(0)) == (False, True)
        # At the edge the shift decides: 9 November on a clock ten hours behind reaches into the 10th…
        assert routes.time_overlap(datetime(2026, 11, 9), "date_only", window, timedelta(hours=-10)) == (None, False)
        # …and on a clock ten hours ahead it ended before the print began.
        assert routes.time_overlap(datetime(2026, 11, 9), "date_only", window, timedelta(hours=10)) == (False, True)
        assert routes.time_overlap(datetime(2026, 11, 11), "date_only", window, timedelta(hours=-10)) == (False, True)


class TestShownVideo:
    """Codex review r1, D7: the chooser offers the video the archive shows, and the job copies that one."""

    async def _local(self, async_client, archive_media):
        answer = await async_client.get(archive_media.sources)
        assert answer.status_code == 200, answer.text
        return answer.json()["local"]["items"]

    async def test_a_video_from_before_the_registry_is_offered_and_downloads(
        self, async_client, archive_media, db_session
    ):
        from sqlalchemy import delete, func, select

        from backend.app.models.archive_media_asset import ArchiveMediaAsset

        await db_session.execute(
            delete(ArchiveMediaAsset).where(ArchiveMediaAsset.archive_id == archive_media.archive.id)
        )
        await db_session.commit()
        shown = await async_client.get(archive_media.sources.replace("/media-sources", "/timelapse"))
        assert shown.status_code == 200 and shown.content == b"local-video" * 300
        items = await self._local(async_client, archive_media)
        assert [(i["name"], i["size"]) for i in items] == [("Lamp.mp4", 3300)]
        assert items[0]["asset"] != archive_media.asset and "archive/" not in items[0]["asset"]

        created = await async_client.post(archive_media.base, json={"assets": [items[0]["asset"]]})
        assert created.status_code == 202, created.text
        job_id = created.json()["job_id"]
        await _until(async_client, f"{archive_media.base}/{job_id}")
        artifact = await async_client.get(f"{archive_media.base}/{job_id}/artifact")
        assert artifact.content == b"local-video" * 300
        # Read only: the chooser and the job wrote no row for it.
        rows = await db_session.scalar(select(func.count()).select_from(ArchiveMediaAsset))
        assert rows == 0

    async def test_a_pointer_the_registry_does_not_describe_is_what_is_offered(
        self, async_client, archive_media, db_session, tmp_path
    ):
        from backend.app.models.archive import PrintArchive

        # An older writer replaced the video and left the registry's row behind.
        relative = "archive/1/clip/newer.mp4"
        (tmp_path / relative).write_bytes(b"newer-video")
        row = await db_session.get(PrintArchive, archive_media.archive.id)
        row.timelapse_path = relative
        await db_session.commit()
        items = await self._local(async_client, archive_media)
        assert [i["size"] for i in items] == [len(b"newer-video")]
        assert items[0]["asset"] != archive_media.asset
        # The stale row's video is not brought back under its old id.
        assert (await async_client.post(archive_media.base, json={"assets": [archive_media.asset]})).status_code == 404
        created = await async_client.post(archive_media.base, json={"assets": [items[0]["asset"]]})
        job_id = created.json()["job_id"]
        await _until(async_client, f"{archive_media.base}/{job_id}")
        assert (await async_client.get(f"{archive_media.base}/{job_id}/artifact")).content == b"newer-video"

    async def test_a_pointer_that_leaves_the_data_directory_offers_nothing(
        self, async_client, archive_media, db_session
    ):
        from backend.app.models.archive import PrintArchive

        row = await db_session.get(PrintArchive, archive_media.archive.id)
        row.timelapse_path = "../outside.mp4"
        await db_session.commit()
        assert await self._local(async_client, archive_media) == []

    async def _waiting_local_job(self, async_client, archive_media, monkeypatch):
        """A job of the attached video that waits behind another one."""
        monkeypatch.setattr(media_downloads, "MAX_ACTIVE", 1)

        archive_media.transport.files["/timelapse/hold.mp4"] = b"hold"
        archive_media.transport.block()
        remote = {"printer_id": archive_media.printer.id, "storage": "external", "path": "/timelapse/hold.mp4"}
        assert (await async_client.post(archive_media.base, json={"remote": [remote]})).status_code == 202
        await asyncio.wait_for(archive_media.transport.entered.wait(), 3)
        created = await async_client.post(archive_media.base, json={"assets": [archive_media.asset]})
        assert created.status_code == 202, created.text
        job = media_download_runtime.jobs[created.json()["job_id"]]
        assert job.phase == "queued"
        return job

    async def _let_it_run(self, archive_media, job):
        archive_media.transport.worker_ended.set()
        archive_media.transport.hold.set()
        await asyncio.wait_for(job.done.wait(), 5)

    async def test_a_file_put_in_the_videos_name_while_the_job_waited_is_left_out(
        self, async_client, archive_media, monkeypatch, tmp_path
    ):
        job = await self._waiting_local_job(async_client, archive_media, monkeypatch)
        target = tmp_path / "archive/1/clip/timelapse.mp4"
        staged = target.with_name(".new.part")
        staged.write_bytes(b"another-video" * 300)
        staged.replace(target)  # the registry has not caught up: same pointer, same row
        await self._let_it_run(archive_media, job)
        assert (job.phase, job.error) == ("failed", "nothing_downloaded")
        assert [(r["status"], r.get("reason")) for r in job.results] == [("not_readable", "replaced")]

    async def test_a_video_replaced_through_the_registry_while_the_job_waited_is_left_out(
        self, async_client, archive_media, monkeypatch, db_session, tmp_path
    ):
        from backend.app.models.archive import PrintArchive

        job = await self._waiting_local_job(async_client, archive_media, monkeypatch)
        (tmp_path / "archive/1/clip/newer.mp4").write_bytes(b"newer-video")
        row = await db_session.get(PrintArchive, archive_media.archive.id)
        row.timelapse_path = "archive/1/clip/newer.mp4"
        await db_session.commit()
        await self._let_it_run(archive_media, job)
        assert (job.phase, job.error) == ("failed", "nothing_downloaded")
        assert job.results[0].get("reason") == "replaced"

    async def test_a_video_removed_while_the_job_waited_is_left_out(
        self, async_client, archive_media, monkeypatch, db_session
    ):
        from backend.app.models.archive import PrintArchive

        job = await self._waiting_local_job(async_client, archive_media, monkeypatch)
        row = await db_session.get(PrintArchive, archive_media.archive.id)
        row.timelapse_path = None
        await db_session.commit()
        await self._let_it_run(archive_media, job)
        assert (job.phase, job.error) == ("failed", "nothing_downloaded")


class TestCard:
    """Codex review r1, D8: a card the printer has not reported is asked, not assumed absent."""

    async def _camera(self, async_client, archive_media, monkeypatch, *, seen, state_value):
        from backend.app.services.bambu_mqtt import PrinterState
        from backend.app.services.printer_manager import printer_manager

        state = PrinterState()
        state.sdcard_state, state.sdcard_state_seen = state_value, seen
        monkeypatch.setattr(printer_manager, "get_status", lambda *_a: state)
        answer = await async_client.get(archive_media.sources)
        assert answer.status_code == 200, answer.text
        return answer.json()["camera"]["status"]

    async def test_a_state_that_never_reported_the_card_is_not_no_card(self, async_client, archive_media, monkeypatch):
        from backend.app.utils.printer_storage import SDCARD_NONE

        # The default reads "none" before the first report; the card answers "no such folder".
        assert (
            await self._camera(async_client, archive_media, monkeypatch, seen=False, state_value=SDCARD_NONE) == "empty"
        )

    async def test_a_printer_that_reported_no_card_has_no_camera_catalogue(
        self, async_client, archive_media, monkeypatch
    ):
        from backend.app.utils.printer_storage import SDCARD_NONE

        status = await self._camera(async_client, archive_media, monkeypatch, seen=True, state_value=SDCARD_NONE)
        assert status == "unsupported"

    async def test_a_reported_card_whose_printer_does_not_answer_is_unavailable(
        self, async_client, archive_media, monkeypatch
    ):
        from backend.app.utils.printer_storage import SDCARD_NORMAL

        archive_media.listings.camera = ListingAnswer("unavailable")
        status = await self._camera(async_client, archive_media, monkeypatch, seen=True, state_value=SDCARD_NORMAL)
        assert status == "unavailable"


class TestJobRequirements:
    """Codex review r1, D1: an archive's route asks for the archive; the job asks for what it read."""

    async def test_a_job_names_what_it_reads(self, async_client, archive_media):
        # The composition mutation M11 could not see through the coordinator's second protection.
        archive_media.transport.files["/timelapse/a.mp4"] = b"a"
        local = await async_client.post(archive_media.base, json={"assets": [archive_media.asset]})
        remote_file = {"printer_id": archive_media.printer.id, "storage": "external", "path": "/timelapse/a.mp4"}
        remote = await async_client.post(archive_media.base, json={"remote": [remote_file]})
        assert (local.status_code, remote.status_code) == (202, 202)
        own = media_download_runtime.jobs[local.json()["job_id"]].scope.descriptor
        assert own.permissions == ("archives:read_all",)
        assert (own.printer_id, own.printer_identity, own.resource_printer_scope) == (None, None, False)
        assert (own.resource_kind, own.resource_id) == ("archive", archive_media.archive.id)
        reads = media_download_runtime.jobs[remote.json()["job_id"]].scope.descriptor
        assert set(reads.permissions) == {"archives:read_all", "printers:files"}
        assert (reads.printer_id, reads.printer_identity) == (
            archive_media.printer.id,
            archive_media.printer.connector_identity,
        )
        assert (reads.resource_kind, reads.resource_id, reads.resource_stamp) == (
            "archive",
            archive_media.archive.id,
            own.resource_stamp,
        )

    async def test_the_header_door_sends_a_recording_under_the_jobs_own_requirements(
        self, async_client, archive_media, monkeypatch
    ):
        from backend.app.services.live_authority import live_authority

        archive_media.transport.files["/timelapse/a.mp4"] = b"recording"
        remote = {"printer_id": archive_media.printer.id, "storage": "external", "path": "/timelapse/a.mp4"}
        created = await async_client.post(archive_media.base, json={"remote": [remote]})
        job = media_download_runtime.jobs[created.json()["job_id"]]
        await asyncio.wait_for(job.done.wait(), 8)
        seen = []
        register = live_authority.register

        def observing(descriptor):
            seen.append(descriptor)
            return register(descriptor)

        monkeypatch.setattr(live_authority, "register", observing)
        answer = await async_client.get(f"{archive_media.base}/{job.job_id}/artifact")
        assert answer.status_code == 200 and answer.content == b"recording"
        sent_under, wanted = seen[-1], job.scope.descriptor
        assert (sent_under.printer_id, sent_under.printer_identity, set(sent_under.permissions)) == (
            wanted.printer_id,
            wanted.printer_identity,
            set(wanted.permissions),
        )
        assert (sent_under.resource_kind, sent_under.resource_id, sent_under.resource_stamp) == (
            "archive",
            archive_media.archive.id,
            wanted.resource_stamp,
        )

    async def test_a_ready_recording_is_closed_once_the_printer_right_is_gone(
        self, async_client, archive_media, db_session
    ):
        from sqlalchemy import select

        from backend.app.models.group import Group
        from backend.app.services.live_authority import live_authority

        await _user(db_session, "ch_ready", ["archives:read_all", "printers:files", "printers:read"])
        headers = await _jwt("ch_ready")
        base = archive_media.base
        archive_media.transport.files["/timelapse/a.mp4"] = b"recording"
        remote_file = {"printer_id": archive_media.printer.id, "storage": "external", "path": "/timelapse/a.mp4"}
        remote = (await async_client.post(base, json={"remote": [remote_file]}, headers=headers)).json()["job_id"]
        local = (await async_client.post(base, json={"assets": [archive_media.asset]}, headers=headers)).json()[
            "job_id"
        ]
        for job_id in (remote, local):
            assert (await _until(async_client, f"{base}/{job_id}", headers=headers))["phase"] == "ready"
        minted = await async_client.post(f"{base}/{remote}/download-token", json={}, headers=headers)
        assert minted.status_code == 200, minted.text
        calls = len(archive_media.transport.calls)

        group = (await db_session.execute(select(Group).where(Group.name == "grp-ch_ready"))).scalar_one()
        group.permissions = ["archives:read_all"]
        await db_session.commit()
        live_authority.invalidate()

        # The archive's own route gate still lets this caller in; the recording's job does not.
        for method, path in (
            ("GET", f"{base}/{remote}"),
            ("DELETE", f"{base}/{remote}"),
            ("POST", f"{base}/{remote}/download-token"),
            ("GET", f"{base}/{remote}/artifact"),
        ):
            answer = await async_client.request(method, path, headers=headers)
            assert answer.status_code == 403, (method, path, answer.text)
            assert b"recording" not in answer.content
        native = await async_client.get(minted.json()["url"], headers={"Authorization": ""})
        assert native.status_code == 403 and b"recording" not in native.content
        assert [j["job_id"] for j in (await async_client.get(base, headers=headers)).json()["jobs"]] == [local]
        # The attached video is the archive's own: it needs no printer.
        assert (await async_client.get(f"{base}/{local}", headers=headers)).json()["phase"] == "ready"
        artifact = await async_client.get(f"{base}/{local}/artifact", headers=headers)
        assert artifact.status_code == 200 and artifact.content == b"local-video" * 300
        assert len(archive_media.transport.calls) == calls

    async def test_an_archive_that_took_the_id_does_not_inherit_the_job(self, async_client, archive_media, db_session):
        import uuid as uuid_module

        from backend.app.models.archive import PrintArchive
        from backend.app.services.live_authority import live_authority

        base = archive_media.base
        job_id = (await async_client.post(base, json={"assets": [archive_media.asset]})).json()["job_id"]
        await _until(async_client, f"{base}/{job_id}")
        row = await db_session.get(PrintArchive, archive_media.archive.id)
        row.recovery_identity = str(uuid_module.uuid4())  # the same number, another print
        await db_session.commit()
        live_authority.invalidate()
        for path, expected in ((f"{base}/{job_id}", 404), (f"{base}/{job_id}/artifact", 401)):
            answer = await async_client.get(path)
            assert answer.status_code == expected, (path, answer.text)
            assert b"local-video" not in answer.content
        assert (await async_client.get(base)).json() == {"jobs": []}
