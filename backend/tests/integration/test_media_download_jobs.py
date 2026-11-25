"""Download jobs for a printer's files, through the real application (G10 D, M15–M19, M22).

The printer is a fake transport with the real ``download_to_file`` contract;
everything else is the application: the routes and their gates, the job
runtime, live authority, the one-time grant and the final delivery gate.
"""

from __future__ import annotations

import asyncio
import io
import tracemalloc
import zipfile
from types import SimpleNamespace

import pytest
from httpx import AsyncClient

from backend.app.services import media_downloads
from backend.app.services.media_downloads import media_download_runtime
from backend.app.services.printer_files.base import TransferOutcome
from backend.tests.integration.test_workshop_library_rights import _jwt, _key, _user

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(autouse=True)
def _patient_authority(monkeypatch):
    """A loaded machine is not a lost authority.

    The coordinator gives each check one second and fails closed after it. Every session of a test
    shares one connection, so a check cancelled by that deadline also closes the database under the
    test. Retirement is tested here by a real revocation, never by the deadline.
    """
    from backend.app.services import live_authority

    monkeypatch.setattr(live_authority, "CHECK_SECONDS", 30.0)


class FakeTransport:
    """A printer's files as ``{path: bytes}``; a ``str`` value is the status that path answers with."""

    def __init__(self, files):
        self.files = dict(files)
        self.calls: list[dict] = []
        self.hold: asyncio.Event | None = None
        self.entered = asyncio.Event()
        self.cancel_seen = asyncio.Event()
        self.worker_ended = asyncio.Event()
        self.worker_ended.set()

    def block(self):
        """The next transfer waits until ``hold`` is set; a cancel goes on only after ``worker_ended``."""
        self.hold = asyncio.Event()
        self.worker_ended.clear()

    async def download_to_file(
        self, path, destination, *, byte_budget=None, reserve_bytes=0, progress=None, join_on_cancel=False
    ):
        self.calls.append({"path": path, "budget": byte_budget, "reserve": reserve_bytes, "join": join_on_cancel})
        entry = self.files.get(path)
        if entry is None:
            return TransferOutcome("not_readable", reason="550")
        if isinstance(entry, str):
            return TransferOutcome(entry, reason="fake")
        if self.hold is not None:
            self.entered.set()
            try:
                await self.hold.wait()
            except asyncio.CancelledError:
                # As the real transports with join_on_cancel: the cancel resumes the
                # caller only once the worker has really ended.
                self.cancel_seen.set()
                while not self.worker_ended.is_set():
                    try:
                        await self.worker_ended.wait()
                    except asyncio.CancelledError:
                        continue
                raise
        size = entry if isinstance(entry, int) else len(entry)
        if byte_budget is not None and size > byte_budget:
            return TransferOutcome("limit_exceeded", byte_budget, reason="byte_budget")
        with destination.open("wb") as handle:
            if isinstance(entry, int):
                handle.truncate(entry)  # a large logical file without writing it
            else:
                handle.write(entry)
        if progress is not None:
            progress(size)
        return TransferOutcome("completed", size, "size_verified")


@pytest.fixture
async def printer_files(monkeypatch, tmp_path, async_client, printer_factory):
    """One printer whose files are the fake transport's; the job runtime on a scratch of its own."""
    transport = FakeTransport({})
    monkeypatch.setattr(
        "backend.app.services.printer_files.factory.transport_for", lambda _printer, _storage: transport
    )
    monkeypatch.setattr("backend.app.api.routes.printers._resolve_storage", lambda *_a, **_k: "external")
    monkeypatch.setattr(media_downloads, "_base_reserve", lambda _usage: 0)
    media_download_runtime.reopen()
    media_download_runtime.root = tmp_path / "media-downloads"
    printer = await printer_factory()
    box = SimpleNamespace(
        transport=transport, printer=printer, base=f"/api/v1/printers/{printer.id}/files/download-jobs"
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


async def _until(client: AsyncClient, url: str, phases=("ready", "failed", "cancelled"), headers=None, seconds=8.0):
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        response = await client.get(url, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        if body["phase"] in phases:
            return body
        assert asyncio.get_running_loop().time() < deadline, body
        await asyncio.sleep(0.02)


async def _start(client, box, paths, **extra):
    response = await client.post(box.base, json={"paths": paths, **extra})
    assert response.status_code == 202, response.text
    return response.json()


async def _grant_url(client, box, job_id, **body):
    response = await client.post(f"{box.base}/{job_id}/download-token", json=body)
    assert response.status_code == 200, response.text
    return response.json()["url"]


def _scratch_entries(box):
    root = media_download_runtime.root
    return sorted(p.name for p in root.iterdir()) if root.exists() else []


class TestOneFile:
    async def test_a_file_is_prepared_on_disk_and_saved_by_a_one_time_url(self, async_client, printer_files):
        printer_files.transport.files["/timelapse/video one.mp4"] = b"video-bytes" * 1000
        created = await _start(async_client, printer_files, ["/timelapse/video one.mp4"])
        assert created["phase"] in ("queued", "preparing", "ready")
        assert created["requested"] == 1 and created["as_zip"] is False

        ready = await _until(async_client, f"{printer_files.base}/{created['job_id']}")
        assert (ready["phase"], ready["succeeded"], ready["failed"], ready["partial"]) == ("ready", 1, 0, False)
        assert ready["filename"] == "video one.mp4" and ready["size"] == 11000
        assert printer_files.transport.calls[0]["join"] is True

        url = await _grant_url(async_client, printer_files, created["job_id"])
        assert url.endswith("/video%20one.mp4")
        # The browser's navigation: no Authorization header.
        answer = await async_client.get(url, headers={"Authorization": ""})
        assert answer.status_code == 200
        assert answer.content == b"video-bytes" * 1000
        assert answer.headers["accept-ranges"] == "none"
        assert "video%20one.mp4" in answer.headers["content-disposition"]
        assert answer.headers["cache-control"] == "no-store"

    async def test_a_grant_is_spent_by_its_first_use(self, async_client, printer_files):
        printer_files.transport.files["/a.gcode"] = b"G1 X1\n"
        job = await _start(async_client, printer_files, ["/a.gcode"])
        await _until(async_client, f"{printer_files.base}/{job['job_id']}")
        url = await _grant_url(async_client, printer_files, job["job_id"])

        assert (await async_client.get(url, headers={"Authorization": ""})).status_code == 200
        again = await async_client.get(url, headers={"Authorization": ""})
        # A refusal on this door is a small text file, never JSON saved under the file's name.
        assert again.status_code == 401
        assert again.headers["content-type"].startswith("text/plain")
        assert 'filename="download-failed.txt"' in again.headers["content-disposition"]
        assert "{" not in again.text

        # The job is still there: a fresh grant works.
        url2 = await _grant_url(async_client, printer_files, job["job_id"])
        assert url2 != url
        assert (await async_client.get(url2, headers={"Authorization": ""})).content == b"G1 X1\n"

    async def test_range_is_ignored_and_head_spends_nothing(self, async_client, printer_files):
        printer_files.transport.files["/a.bin"] = bytes(range(256)) * 40
        job = await _start(async_client, printer_files, ["/a.bin"])
        await _until(async_client, f"{printer_files.base}/{job['job_id']}")
        url = await _grant_url(async_client, printer_files, job["job_id"])

        head = await async_client.head(url, headers={"Authorization": ""})
        assert head.status_code == 405 and head.headers["allow"] == "GET"

        for value in ("bytes=10-19", "bytes=0-0,5-9", "bytes=999999-", "nonsense"):
            url = await _grant_url(async_client, printer_files, job["job_id"])
            ranged = await async_client.get(url, headers={"Authorization": "", "Range": value, "If-Range": '"x"'})
            assert ranged.status_code == 200, value
            assert "content-range" not in ranged.headers
            assert ranged.headers["accept-ranges"] == "none"
            assert ranged.content == bytes(range(256)) * 40

    async def test_the_url_name_must_be_the_jobs_own(self, async_client, printer_files):
        printer_files.transport.files["/a.gcode"] = b"x"
        job = await _start(async_client, printer_files, ["/a.gcode"])
        await _until(async_client, f"{printer_files.base}/{job['job_id']}")
        url = await _grant_url(async_client, printer_files, job["job_id"])
        other = url.rsplit("/", 1)[0] + "/other.zip"
        assert (await async_client.get(other, headers={"Authorization": ""})).status_code == 404

    async def test_a_missing_file_fails_the_job_and_leaves_no_scratch(self, async_client, printer_files):
        job = await _start(async_client, printer_files, ["/gone.3mf"])
        failed = await _until(async_client, f"{printer_files.base}/{job['job_id']}")
        assert (failed["phase"], failed["error"]) == ("failed", "nothing_downloaded")
        assert failed["failures"] == [{"name": "gone.3mf", "status": "not_readable", "reason": "550"}]
        assert _scratch_entries(printer_files) == []
        refused = await async_client.post(f"{printer_files.base}/{job['job_id']}/download-token", json={})
        assert refused.status_code == 409 and refused.json()["detail"]["error"] == "download_not_ready"


class TestZip:
    async def test_a_partial_archive_needs_a_confirmation_and_holds_what_succeeded(self, async_client, printer_files):
        printer_files.transport.files.update(
            {
                "/a/part.gcode": b"first" * 500,
                "/b/part.gcode": b"second",
                "/model.3mf": b"PK-model",
                "/slow.mp4": "stalled",
            }
        )
        paths = ["/a/part.gcode", "/b/part.gcode", "/model.3mf", "/slow.mp4", "/absent.txt"]
        job = await _start(async_client, printer_files, paths, as_zip=True, filename="../my files")
        ready = await _until(async_client, f"{printer_files.base}/{job['job_id']}")
        assert (ready["phase"], ready["requested"], ready["succeeded"], ready["failed"]) == ("ready", 5, 3, 2)
        assert ready["partial"] is True and ready["filename"] == "my files.zip"
        assert {(f["name"], f["status"]) for f in ready["failures"]} == {
            ("slow.mp4", "stalled"),
            ("absent.txt", "not_readable"),
        }

        by_header = await async_client.get(f"{printer_files.base}/{job['job_id']}/artifact")
        assert by_header.status_code == 409 and by_header.json()["detail"]["error"] == "partial_not_confirmed"
        unconfirmed = await async_client.post(f"{printer_files.base}/{job['job_id']}/download-token", json={})
        assert unconfirmed.status_code == 409
        assert unconfirmed.json()["detail"]["error"] == "partial_not_confirmed"

        url = await _grant_url(async_client, printer_files, job["job_id"], accept_partial=True)
        answer = await async_client.get(url, headers={"Authorization": ""})
        assert answer.status_code == 200 and answer.headers["content-type"] == "application/zip"
        with zipfile.ZipFile(io.BytesIO(answer.content)) as archive:
            assert archive.testzip() is None
            # Deterministic duplicate suffixes; no directory of the printer leaks in.
            assert archive.namelist() == ["part.gcode", "part (2).gcode", "model.3mf"]
            assert archive.read("part.gcode") == b"first" * 500
            assert archive.read("part (2).gcode") == b"second"
            kinds = {info.filename: info.compress_type for info in archive.infolist()}
            assert kinds["part.gcode"] == zipfile.ZIP_DEFLATED and kinds["model.3mf"] == zipfile.ZIP_STORED
        # Raw copies went as their entries were closed.
        job_dir = media_download_runtime.root / job["job_id"]
        assert sorted(p.name for p in (job_dir / "raw").iterdir()) == []

    async def test_several_files_are_always_an_archive(self, async_client, printer_files):
        printer_files.transport.files.update({"/a.txt": b"a", "/b.txt": b"b"})
        job = await _start(async_client, printer_files, ["/a.txt", "/b.txt"])
        assert job["as_zip"] is True and job["filename"].endswith(".zip")

    async def test_the_byte_budget_ends_the_whole_job(self, async_client, printer_files, monkeypatch):
        monkeypatch.setattr(media_downloads, "BYTE_BUDGET", 10)
        printer_files.transport.files.update({"/a.txt": b"123456", "/b.txt": b"123456", "/c.txt": b"1"})
        job = await _start(async_client, printer_files, ["/a.txt", "/b.txt", "/c.txt"], as_zip=True)
        failed = await _until(async_client, f"{printer_files.base}/{job['job_id']}")
        # A fatal limit is not worked around by what did succeed.
        assert (failed["phase"], failed["error"], failed["size"]) == ("failed", "limit_exceeded", None)
        assert [c["budget"] for c in printer_files.transport.calls] == [10, 4]
        assert _scratch_entries(printer_files) == []

    async def test_a_last_file_over_the_budget_is_fatal_too(self, async_client, printer_files, monkeypatch):
        # Nothing is asked for after it, so only the transfer's own answer can end the job.
        monkeypatch.setattr(media_downloads, "BYTE_BUDGET", 10)
        printer_files.transport.files.update({"/a.txt": b"123456", "/b.txt": b"123456"})
        job = await _start(async_client, printer_files, ["/a.txt", "/b.txt"], as_zip=True)
        failed = await _until(async_client, f"{printer_files.base}/{job['job_id']}")
        assert (failed["phase"], failed["error"], failed["size"]) == ("failed", "limit_exceeded", None)
        assert _scratch_entries(printer_files) == []

    async def test_an_unavailable_printer_is_not_tried_for_every_file(self, async_client, printer_files):
        printer_files.transport.files.update({f"/f{i}.txt": "unavailable" for i in range(8)})
        job = await _start(async_client, printer_files, [f"/f{i}.txt" for i in range(8)], as_zip=True)
        failed = await _until(async_client, f"{printer_files.base}/{job['job_id']}")
        assert failed["error"] == "nothing_downloaded"
        assert len(printer_files.transport.calls) == media_downloads.UNAVAILABLE_STREAK
        assert [f["status"] for f in failed["failures"]].count(
            "not_attempted"
        ) == 8 - media_downloads.UNAVAILABLE_STREAK


class TestRequest:
    @pytest.mark.parametrize(
        "paths",
        [[], ["relative.txt"], ["/a/../b.txt"], ["/dir/"], ["/a.txt", "/a.txt"], ["/a\nb.txt"], ["/" + "x" * 2000]],
    )
    async def test_a_selection_is_validated_before_anything_is_read(self, async_client, printer_files, paths):
        response = await async_client.post(printer_files.base, json={"paths": paths})
        assert response.status_code == 422
        assert printer_files.transport.calls == []

    async def test_admission_refuses_when_the_volume_has_no_room(self, async_client, printer_files, monkeypatch):
        monkeypatch.setattr(media_downloads, "_base_reserve", lambda usage: usage.free + 1)
        response = await async_client.post(printer_files.base, json={"paths": ["/a.txt"]})
        assert response.status_code == 507
        assert response.json()["detail"]["error"] == "insufficient_storage"
        assert _scratch_entries(printer_files) == []

    async def test_the_queue_is_bounded_per_caller(self, async_client, printer_files):
        printer_files.transport.files.update({f"/f{i}.txt": b"x" for i in range(6)})
        printer_files.transport.block()
        # All four at once: a job is "queued" until its task takes a slot, and a burst
        # must not be refused while slots are free.
        jobs = await asyncio.gather(*(_start(async_client, printer_files, [f"/f{i}.txt"]) for i in range(4)))
        await asyncio.wait_for(printer_files.transport.entered.wait(), 3)
        listed = (await async_client.get(printer_files.base)).json()["jobs"]
        assert sorted(j["job_id"] for j in listed) == sorted(j["job_id"] for j in jobs)
        assert sorted(j["phase"] for j in listed) == ["preparing", "preparing", "queued", "queued"]
        refused = await async_client.post(printer_files.base, json={"paths": ["/f5.txt"]})
        assert refused.status_code == 429 and refused.json()["detail"]["error"] == "download_user_limit"
        assert refused.headers["retry-after"] == "30"

    async def test_the_one_request_downloads_wait_in_the_same_bounded_queue(
        self, async_client, printer_files, monkeypatch
    ):
        monkeypatch.setattr(media_downloads, "MAX_WAITING", 1)
        printer_files.transport.files.update({f"/f{i}.txt": b"x" for i in range(5)})
        printer_files.transport.block()
        url = f"/api/v1/printers/{printer_files.printer.id}/files/download"
        held = [asyncio.create_task(async_client.get(url, params={"path": f"/f{i}.txt"})) for i in range(3)]
        await asyncio.wait_for(printer_files.transport.entered.wait(), 3)
        for _ in range(100):
            if sum(1 for job in media_download_runtime.jobs.values() if job.waiting) == 1:
                break
            await asyncio.sleep(0.02)
        # Two are being prepared, one waits: the bound is reached, for the old endpoints too.
        refused = await async_client.get(url, params={"path": "/f4.txt"})
        assert refused.status_code == 429 and refused.json()["detail"]["error"] == "download_queue_full"
        printer_files.transport.worker_ended.set()
        printer_files.transport.hold.set()
        answers = await asyncio.gather(*held)
        assert [a.status_code for a in answers] == [200, 200, 200]


class TestCancel:
    async def test_a_job_reads_cancelled_only_once_its_transfer_has_ended(self, async_client, printer_files):
        printer_files.transport.files["/big.mp4"] = b"x" * 10
        printer_files.transport.block()
        job = await _start(async_client, printer_files, ["/big.mp4"])
        url = f"{printer_files.base}/{job['job_id']}"
        await asyncio.wait_for(printer_files.transport.entered.wait(), 3)

        asked = await async_client.delete(url)
        assert asked.status_code == 200 and asked.json()["phase"] == "cancelling"
        await asyncio.wait_for(printer_files.transport.cancel_seen.wait(), 3)
        # The worker is still inside the transfer: not cancelled yet, scratch still owned.
        for _ in range(5):
            await asyncio.sleep(0.02)
            assert (await async_client.get(url)).json()["phase"] == "cancelling"
        assert _scratch_entries(printer_files) == [job["job_id"]]
        # A second cancel changes nothing.
        assert (await async_client.delete(url)).json()["phase"] == "cancelling"

        printer_files.transport.worker_ended.set()
        done = await _until(async_client, url)
        assert (done["phase"], done["error"]) == ("cancelled", None)
        assert _scratch_entries(printer_files) == []

    async def test_deleting_a_ready_job_drops_its_result(self, async_client, printer_files):
        printer_files.transport.files["/a.txt"] = b"a"
        job = await _start(async_client, printer_files, ["/a.txt"])
        url = f"{printer_files.base}/{job['job_id']}"
        await _until(async_client, url)
        grant = await _grant_url(async_client, printer_files, job["job_id"])
        assert (await async_client.delete(url)).status_code == 200
        for _ in range(100):
            if (await async_client.get(url)).status_code == 404:
                break
            await asyncio.sleep(0.02)
        assert (await async_client.get(url)).status_code == 404
        assert _scratch_entries(printer_files) == []
        # The grant minted before the delete opens nothing.
        assert (await async_client.get(grant, headers={"Authorization": ""})).status_code == 404


class TestWhoMayRead:
    async def test_another_users_job_is_the_same_404_as_a_missing_one(self, async_client, printer_files, db_session):
        printer_files.transport.files["/a.txt"] = b"a"
        job = await _start(async_client, printer_files, ["/a.txt"])
        await _until(async_client, f"{printer_files.base}/{job['job_id']}")
        await _user(db_session, "dl_other", ["printers:files", "printers:read"])
        other = await _jwt("dl_other")
        base = printer_files.base
        missing = await async_client.get(f"{base}/{'0' * 32}", headers=other)
        assert missing.status_code == 404
        for method, path in (
            ("GET", f"{base}/{job['job_id']}"),
            ("DELETE", f"{base}/{job['job_id']}"),
            ("POST", f"{base}/{job['job_id']}/download-token"),
            ("GET", f"{base}/{job['job_id']}/artifact"),
        ):
            answer = await async_client.request(method, path, headers=other)
            assert (answer.status_code, answer.json()) == (404, missing.json()), (method, path)
        assert (await async_client.get(base, headers=other)).json() == {"jobs": []}
        # The owner's job is untouched.
        assert (await async_client.get(f"{base}/{job['job_id']}")).json()["phase"] == "ready"

    async def test_a_caller_without_the_right_is_refused(self, async_client, printer_files, db_session):
        await _user(db_session, "dl_viewer", ["printers:read"])
        refused = await async_client.post(
            printer_files.base, json={"paths": ["/a.txt"]}, headers=await _jwt("dl_viewer")
        )
        assert refused.status_code == 403
        assert printer_files.transport.calls == []

    async def test_an_api_key_takes_the_artifact_by_header_and_gets_no_url(
        self, async_client, printer_files, db_session, printer_factory
    ):
        owner = await _user(db_session, "dl_key_owner", ["printers:files", "printers:read"])
        raw = await _key(db_session, owner, can_control_printer=True, printer_ids=[printer_files.printer.id])
        key = {"X-API-Key": raw, "Authorization": ""}
        printer_files.transport.files["/a.txt"] = b"key-bytes"
        created = await async_client.post(printer_files.base, json={"paths": ["/a.txt"]}, headers=key)
        assert created.status_code == 202, created.text
        job_id = created.json()["job_id"]
        await _until(async_client, f"{printer_files.base}/{job_id}", headers=key)

        # GP2/OD-5: a one-time URL is a browser session's; a key is refused.
        minted = await async_client.post(f"{printer_files.base}/{job_id}/download-token", json={}, headers=key)
        assert minted.status_code == 403

        artifact = await async_client.get(
            f"{printer_files.base}/{job_id}/artifact", headers={**key, "Range": "bytes=0-2"}
        )
        assert artifact.status_code == 200 and artifact.content == b"key-bytes"
        assert artifact.headers["accept-ranges"] == "none"

        # The same owner's browser session does not see the key's job, and the other way round.
        assert (
            await async_client.get(f"{printer_files.base}/{job_id}", headers=await _jwt("dl_key_owner"))
        ).status_code == 404

        # A key limited to this printer cannot start a job on another one.
        other_printer = await printer_factory(name="Other", serial_number="OTHER0001")
        elsewhere = await async_client.post(
            f"/api/v1/printers/{other_printer.id}/files/download-jobs", json={"paths": ["/a.txt"]}, headers=key
        )
        assert elsewhere.status_code == 403
        # …and its job is not reachable through another printer's path.
        crossed = await async_client.get(f"/api/v1/printers/{other_printer.id}/files/download-jobs/{job_id}")
        assert crossed.status_code == 404

    async def test_a_caller_who_loses_access_stops_the_job_and_nothing_is_handed_out(
        self, async_client, printer_files, db_session
    ):
        from backend.app.models.user import User
        from backend.app.services.live_authority import live_authority

        user = await _user(db_session, "dl_revoked", ["printers:files", "printers:read"])
        headers = await _jwt("dl_revoked")
        printer_files.transport.files["/a.txt"] = b"a"
        printer_files.transport.block()
        created = await async_client.post(printer_files.base, json={"paths": ["/a.txt"]}, headers=headers)
        job = media_download_runtime.jobs[created.json()["job_id"]]
        await asyncio.wait_for(printer_files.transport.entered.wait(), 3)

        row = await db_session.get(User, user.id)
        row.is_active = False
        await db_session.commit()
        live_authority.invalidate()
        await asyncio.wait_for(printer_files.transport.cancel_seen.wait(), 6)
        printer_files.transport.worker_ended.set()
        await asyncio.wait_for(job.done.wait(), 3)
        assert (job.phase, job.error, job.artifact) == ("failed", "authority_lost", None)
        assert _scratch_entries(printer_files) == []


class TestRestore:
    async def test_a_restore_stops_every_job_and_closes_admission(self, async_client, printer_files):
        printer_files.transport.files.update({"/a.txt": b"a", "/b.txt": b"b"})
        ready = await _start(async_client, printer_files, ["/a.txt"])
        await _until(async_client, f"{printer_files.base}/{ready['job_id']}")
        grant = await _grant_url(async_client, printer_files, ready["job_id"])
        printer_files.transport.block()
        running = await _start(async_client, printer_files, ["/b.txt"])
        job = media_download_runtime.jobs[running["job_id"]]
        await asyncio.wait_for(printer_files.transport.entered.wait(), 3)

        drain = asyncio.create_task(media_download_runtime.quiesce(5))
        await asyncio.wait_for(printer_files.transport.cancel_seen.wait(), 3)
        assert not drain.done()  # the worker has not ended: no proof yet
        printer_files.transport.worker_ended.set()
        assert await drain is True
        assert (job.phase, job.error) == ("failed", "interrupted")
        assert _scratch_entries(printer_files) == []
        assert media_download_runtime.jobs == {}

        refused = await async_client.post(printer_files.base, json={"paths": ["/a.txt"]})
        assert refused.status_code == 503 and refused.json()["detail"]["error"] == "downloads_unavailable"
        # A grant of the earlier generation opens nothing.
        assert (await async_client.get(grant, headers={"Authorization": ""})).status_code == 404

    async def test_an_unproven_drain_is_refused(self, async_client, printer_files):
        printer_files.transport.files["/a.txt"] = b"a"
        printer_files.transport.block()
        await _start(async_client, printer_files, ["/a.txt"])
        await asyncio.wait_for(printer_files.transport.entered.wait(), 3)
        assert await media_download_runtime.quiesce(0.2) is False
        assert not media_download_runtime.admitting()
        with pytest.raises(RuntimeError):
            media_download_runtime.reopen()


class TestCompatibility:
    async def test_the_single_download_answers_the_file_from_disk(self, async_client, printer_files):
        printer_files.transport.files["/cache/model.3mf"] = b"PK" + b"m" * 5000
        answer = await async_client.get(
            f"/api/v1/printers/{printer_files.printer.id}/files/download", params={"path": "/cache/model.3mf"}
        )
        assert answer.status_code == 200 and answer.content == b"PK" + b"m" * 5000
        assert answer.headers["content-type"] == "application/vnd.ms-package.3dmanufacturing-3dmodel+xml"
        assert "model.3mf" in answer.headers["content-disposition"]
        # Its job was the request's own: gone, with its scratch, once answered.
        for _ in range(100):
            if not _scratch_entries(printer_files):
                break
            await asyncio.sleep(0.02)
        assert _scratch_entries(printer_files) == []
        assert (await async_client.get(printer_files.base)).json() == {"jobs": []}

    async def test_the_single_download_of_a_missing_file_is_404(self, async_client, printer_files):
        answer = await async_client.get(
            f"/api/v1/printers/{printer_files.printer.id}/files/download", params={"path": "/gone.3mf"}
        )
        assert (answer.status_code, answer.json()["detail"]) == (404, "File not found: /gone.3mf")
        assert _scratch_entries(printer_files) == []

    async def test_the_zip_download_keeps_its_shape(self, async_client, printer_files):
        printer_files.transport.files.update({"/a.gcode": b"a" * 100, "/b.gcode": b"b"})
        url = f"/api/v1/printers/{printer_files.printer.id}/files/download-zip"
        answer = await async_client.post(url, json={"paths": ["/a.gcode", "/b.gcode", "/gone.gcode"]})
        assert answer.status_code == 200 and answer.headers["content-type"] == "application/zip"
        assert "printer-files.zip" in answer.headers["content-disposition"]
        assert answer.headers["x-download-failed"] == "1"
        with zipfile.ZipFile(io.BytesIO(answer.content)) as archive:
            assert archive.namelist() == ["a.gcode", "b.gcode"]

        assert (await async_client.post(url, json={"paths": []})).status_code == 400
        nothing = await async_client.post(url, json={"paths": ["/gone.gcode"]})
        assert (nothing.status_code, nothing.json()["detail"]) == (404, "No files could be downloaded")


class TestDelivery:
    """The native door on the raw ASGI application: what really reaches the server-facing send."""

    async def _ready(self, async_client, printer_files, size):
        printer_files.transport.files["/long.mp4"] = size
        job = await _start(async_client, printer_files, ["/long.mp4"])
        await _until(async_client, f"{printer_files.base}/{job['job_id']}")
        return job, await _grant_url(async_client, printer_files, job["job_id"])

    async def test_a_large_download_is_never_held_in_memory(self, async_client, printer_files, monkeypatch):
        from backend.app.main import app
        from backend.app.services import live_authority
        from backend.tests.integration.test_g01_d_media_pipeline import _scope

        # Allocation tracing slows the real admission DB checks on loaded CI
        # runners. Keep admission in the measured path and allow it to finish.
        monkeypatch.setattr(live_authority, "CHECK_SECONDS", 10.0)
        size = 96 * 1024 * 1024
        job, url = await self._ready(async_client, printer_files, size)
        scope = _scope(url, async_client)
        scope["headers"] = [(k, v) for k, v in scope["headers"] if k != b"authorization"]
        sent = 0
        status = []
        asked = False

        async def receive():
            nonlocal asked
            if not asked:
                asked = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.Event().wait()

        async def send(message):
            nonlocal sent
            if message["type"] == "http.response.start":
                status.append(message["status"])
            elif message["type"] == "http.response.body":
                sent += len(message.get("body", b""))

        tracemalloc.start()
        try:
            await asyncio.wait_for(app(scope, receive, send), 60)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert status == [200] and sent == size
        assert peak < 16 * 1024 * 1024, peak
        assert not media_download_runtime.jobs[job["job_id"]].pinned()

    @pytest.mark.parametrize("newline", [False, True])
    async def test_a_retired_session_sends_no_further_body(self, async_client, printer_files, newline):
        from backend.app.main import app
        from backend.tests.integration.test_g01_d_media_pipeline import _backpressure, _scope

        job, url = await self._ready(async_client, printer_files, 8 * 1024 * 1024)
        # ``%0A`` after the name: the router accepts it, so the gate must cover it.
        scope = _scope(url, async_client, newline=newline)
        scope["headers"] = [(k, v) for k, v in scope["headers"] if k != b"authorization"]
        if newline:
            job_object = media_download_runtime.jobs[job["job_id"]]
            task, starts, entered, resume, _ = _backpressure(app, scope)
            # The name in the URL is not the job's (it ends in a newline): refused before any byte.
            with pytest.raises(AssertionError):
                await asyncio.wait_for(task, 5)
            assert starts == [] and not job_object.pinned()
            return
        task, starts, entered, resume, _ = _backpressure(app, scope)
        try:
            await asyncio.wait_for(entered.wait(), 5)
            viewer = scope["state"]["live_media_viewer"]
            assert media_download_runtime.jobs[job["job_id"]].pinned()
            viewer.handle.retire(4401)
            resume.set()
            await asyncio.wait_for(task, 5)
            await asyncio.wait_for(viewer.cleanup, 3)
            assert len(starts) == 1  # the send already entered; nothing after the retirement
            assert viewer.cleanup_done
            # The reader's pin went with its cleanup.
            assert not media_download_runtime.jobs[job["job_id"]].pinned()
        finally:
            resume.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class TestJobRequirements:
    """Codex review r1, D1: a job's own requirements stand on every one of its doors."""

    def _doors(self, base, job_id):
        return (
            ("GET", f"{base}/{job_id}"),
            ("DELETE", f"{base}/{job_id}"),
            ("POST", f"{base}/{job_id}/download-token"),
            ("GET", f"{base}/{job_id}/artifact"),
        )

    async def test_another_session_of_the_same_account_does_not_see_the_job(
        self, async_client, printer_files, db_session
    ):
        await _user(db_session, "dl_two", ["printers:files", "printers:read"])
        first, second = await _jwt("dl_two"), await _jwt("dl_two")
        printer_files.transport.files["/a.txt"] = b"a"
        base = printer_files.base
        created = await async_client.post(base, json={"paths": ["/a.txt"]}, headers=first)
        assert created.status_code == 202, created.text
        job_id = created.json()["job_id"]
        await _until(async_client, f"{base}/{job_id}", headers=first)
        missing = await async_client.get(f"{base}/{'0' * 32}", headers=second)
        for method, path in self._doors(base, job_id):
            answer = await async_client.request(method, path, headers=second)
            assert (answer.status_code, answer.json()) == (404, missing.json()), (method, path)
        assert (await async_client.get(base, headers=second)).json() == {"jobs": []}
        # The session that made it still has it.
        assert [j["job_id"] for j in (await async_client.get(base, headers=first)).json()["jobs"]] == [job_id]
        assert (await async_client.get(f"{base}/{job_id}/artifact", headers=first)).content == b"a"

    async def test_a_printer_that_took_the_id_does_not_inherit_the_job(self, async_client, printer_files, db_session):
        import uuid

        from backend.app.models.printer import Printer
        from backend.app.services.live_authority import live_authority

        printer_files.transport.files["/a.txt"] = b"a"
        base = printer_files.base
        job = await _start(async_client, printer_files, ["/a.txt"])
        await _until(async_client, f"{base}/{job['job_id']}")
        url = await _grant_url(async_client, printer_files, job["job_id"])  # minted while it was still that printer

        # SQLite hands a deleted printer's id to the next one: same number, another machine.
        row = await db_session.get(Printer, printer_files.printer.id)
        row.connector_identity = str(uuid.uuid4())
        await db_session.commit()
        live_authority.invalidate()

        # The state doors answer as for a job that is gone — never 401, which the browser reads
        # as "signed out"; the doors that send the file refuse before their headers.
        for (method, path), expected in zip(self._doors(base, job["job_id"]), (404, 404, 404, 401), strict=True):
            answer = await async_client.request(method, path)
            assert answer.status_code == expected, (method, path, answer.text)
            assert answer.content != b"a"
        assert (await async_client.get(base)).json() == {"jobs": []}
        native = await async_client.get(url, headers={"Authorization": ""})
        assert native.status_code == 401
        assert "download-failed.txt" in native.headers["content-disposition"]
        assert native.content != b"a"

    async def test_an_artifact_door_takes_its_credential_from_the_headers_only(self, async_client, printer_files):
        printer_files.transport.files["/a.txt"] = b"a"
        job = await _start(async_client, printer_files, ["/a.txt"])
        await _until(async_client, f"{printer_files.base}/{job['job_id']}")
        both = await async_client.get(f"{printer_files.base}/{job['job_id']}/artifact", params={"token": "anything"})
        assert both.status_code == 400
        assert (await async_client.get(f"{printer_files.base}/{job['job_id']}/artifact")).content == b"a"

    async def test_a_right_lost_while_waiting_stops_the_job_at_its_first_check(
        self, async_client, printer_files, db_session, monkeypatch
    ):
        # The coordinator retires a RUNNING handle when its caller's rights change. A job that is
        # still waiting has no handle yet: what stops it is the first admission itself, which
        # asks for the permission the job's descriptor names (Codex r1 on mutation M11).
        from sqlalchemy import select

        from backend.app.models.group import Group
        from backend.app.services.live_authority import live_authority

        monkeypatch.setattr(media_downloads, "MAX_ACTIVE", 1)

        await _user(db_session, "dl_waits", ["printers:files", "printers:read"])
        headers = await _jwt("dl_waits")
        printer_files.transport.files.update({"/hold.txt": b"h", "/a.txt": b"a"})
        printer_files.transport.block()
        await _start(async_client, printer_files, ["/hold.txt"])
        await asyncio.wait_for(printer_files.transport.entered.wait(), 3)
        created = await async_client.post(printer_files.base, json={"paths": ["/a.txt"]}, headers=headers)
        assert created.status_code == 202, created.text
        job = media_download_runtime.jobs[created.json()["job_id"]]
        assert job.phase == "queued"

        group = (await db_session.execute(select(Group).where(Group.name == "grp-dl_waits"))).scalar_one()
        group.permissions = ["printers:read"]
        await db_session.commit()
        live_authority.invalidate()
        printer_files.transport.worker_ended.set()
        printer_files.transport.hold.set()
        await asyncio.wait_for(job.done.wait(), 5)
        assert (job.phase, job.error, job.artifact) == ("failed", "authority_lost", None)
        assert [call["path"] for call in printer_files.transport.calls] == ["/hold.txt"]


class TestPromisedRead:
    """Codex review r1, D5: the promise is confirmed before the headers, on the real doors."""

    async def test_a_promise_that_ran_out_is_refused_before_anything_is_sent(
        self, async_client, printer_files, monkeypatch
    ):
        printer_files.transport.files["/a.txt"] = b"a"
        job = await _start(async_client, printer_files, ["/a.txt"])
        await _until(async_client, f"{printer_files.base}/{job['job_id']}")
        url = await _grant_url(async_client, printer_files, job["job_id"])
        # Every promise is already over when it is made: the janitor may have taken the file.
        monkeypatch.setattr(media_downloads, "PENDING_READER_SECONDS", -1.0)
        by_header = await async_client.get(f"{printer_files.base}/{job['job_id']}/artifact")
        assert by_header.status_code == 404 and by_header.content != b"a"
        native = await async_client.get(url, headers={"Authorization": ""})
        assert native.status_code == 404 and "download-failed.txt" in native.headers["content-disposition"]
        assert not media_download_runtime.jobs[job["job_id"]].pinned()

    async def test_a_read_in_progress_holds_the_result_and_releases_it(self, async_client, printer_files):
        from backend.app.main import app
        from backend.tests.integration.test_g01_d_media_pipeline import _scope

        job, url = await TestDelivery()._ready(async_client, printer_files, 4 * 1024 * 1024)
        held = media_download_runtime.jobs[job["job_id"]]
        scope = _scope(url, async_client)
        scope["headers"] = [(k, v) for k, v in scope["headers"] if k != b"authorization"]
        pinned_while_reading = []
        asked = False

        async def receive():
            nonlocal asked
            if not asked:
                asked = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.Event().wait()

        async def send(message):
            if message["type"] == "http.response.body":
                pinned_while_reading.append(any(lease.started for lease in held.leases))

        await asyncio.wait_for(app(scope, receive, send), 30)
        assert pinned_while_reading and all(pinned_while_reading)
        assert not held.pinned() and held.leases == set()


class TestListUnderUncertainAuthority:
    """Codex re-acceptance, D1-R: "could not be read just now" is not "the job is gone"."""

    @pytest.mark.parametrize("code", [403, 404, 503])
    async def test_only_a_proven_refusal_hides_a_job(self, async_client, printer_files, monkeypatch, code):
        from fastapi import HTTPException

        from backend.app.api.routes import media_downloads as routes

        printer_files.transport.files["/a.txt"] = b"a"
        job = await _start(async_client, printer_files, ["/a.txt"])
        await _until(async_client, f"{printer_files.base}/{job['job_id']}")

        async def proof(*_args):
            raise HTTPException(code, "decided by the test", headers={"Retry-After": "5"} if code == 503 else None)

        monkeypatch.setattr(routes, "_prove_job", proof)
        answer = await async_client.get(printer_files.base)
        if code == 503:
            assert (answer.status_code, answer.headers.get("retry-after")) == (503, "5"), answer.text
        else:
            assert answer.status_code == 200 and answer.json() == {"jobs": []}

    async def test_the_coordinators_own_503_reaches_the_list(self, async_client, printer_files, monkeypatch):
        # Not a stand-in for the proof: the coordinator itself cannot read authority.
        from backend.app.services import live_authority as la

        printer_files.transport.files["/a.txt"] = b"a"
        job = await _start(async_client, printer_files, ["/a.txt"])
        await _until(async_client, f"{printer_files.base}/{job['job_id']}")
        monkeypatch.setattr(la, "CHECK_SECONDS", 0.2)

        async def unreadable(_batch):
            await asyncio.Event().wait()

        monkeypatch.setattr(la.live_authority, "_read", unreadable)
        answer = await async_client.get(printer_files.base)
        assert answer.status_code == 503 and answer.headers.get("retry-after")
        assert job["job_id"] in media_download_runtime.jobs  # nothing was dropped
