"""The download job service's own pieces, and the lane rule it leans on (G10 D)."""

from __future__ import annotations

import asyncio
import threading
import time
import zipfile
from pathlib import Path

import pytest

from backend.app.services import media_downloads as md
from backend.app.services.printer_file_lanes import PrinterFileRuntime


class TestNames:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("video.mp4", "video.mp4"),
            ("/timelapse/video.mp4", "video.mp4"),
            ("..\\..\\evil.txt", "evil.txt"),
            ("../../etc/passwd", "passwd"),
            ("..", "download"),
            ("", "download"),
            (None, "download"),
            ('a<b>:"c"|d?*.zip', "a_b___c__d__.zip"),
            ("line\nbreak\x00.txt", "line_break_.txt"),
            ("  spaced .txt. ", "spaced .txt"),
            ("Настільна лампа.3mf", "Настільна лампа.3mf"),
        ],
    )
    def test_a_display_name_is_one_safe_component(self, given, expected):
        assert md.safe_name(given) == expected

    def test_a_long_name_keeps_its_suffix(self):
        name = md.safe_name("x" * 400 + ".gcode")
        assert len(name) == 150 and name.endswith(".gcode")

    def test_duplicates_get_deterministic_suffixes_without_case(self):
        names = ["a.txt", "A.TXT", "a.txt", "b", "b", "a (2).txt"]
        assert md.unique_names(names) == ["a.txt", "A (2).TXT", "a (3).txt", "b", "b (2)", "a (2) (2).txt"]


class TestScratch:
    def test_startup_removes_only_directories_that_carry_our_marker(self, tmp_path):
        root = tmp_path / "media-downloads"
        ours = md._make_job_directory(root, "a" * 32)
        (ours / "raw" / "0000.part").write_bytes(b"left over")
        foreign = root / "somebody-elses"
        foreign.mkdir()
        (foreign / "keep.txt").write_text("keep")
        (root / "plain-file.bin").write_bytes(b"x")
        fake = root / "marker-is-a-directory"
        (fake / md.MARKER).mkdir(parents=True)

        assert md.clean_scratch(root) == (1, 3)
        assert not ours.exists()
        assert (foreign / "keep.txt").read_text() == "keep"
        assert (root / "plain-file.bin").exists() and fake.exists()

    def test_a_missing_root_is_nothing_to_clean(self, tmp_path):
        assert md.clean_scratch(tmp_path / "absent") == (0, 0)

    def test_a_directory_without_the_marker_is_never_removed_as_a_job(self, tmp_path):
        stranger = tmp_path / "not-a-job"
        stranger.mkdir()
        with pytest.raises(OSError):
            md._remove_job_directory(stranger)
        assert stranger.exists()

    def test_a_linked_scratch_root_is_refused(self, tmp_path):
        target = tmp_path / "elsewhere"
        target.mkdir()
        link = tmp_path / "media-downloads"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            pytest.skip("this account may not create symbolic links")
        with pytest.raises(OSError):
            md._make_job_directory(link, "b" * 32)
        assert list(target.iterdir()) == []
        assert md.clean_scratch(link) == (0, 0)


class TestZipEntry:
    def test_an_entry_is_copied_in_chunks_and_a_stop_is_seen(self, tmp_path, monkeypatch):
        monkeypatch.setattr(md, "COPY_CHUNK", 1024)
        raw = tmp_path / "raw.part"
        raw.write_bytes(b"z" * 10_000)
        stop = threading.Event()
        with zipfile.ZipFile(tmp_path / "ok.zip", "w", zipfile.ZIP_DEFLATED, True) as archive:
            assert md._zip_add(archive, raw, "clip.mp4", stop, 0) == 10_000
        with zipfile.ZipFile(tmp_path / "ok.zip") as archive:
            assert archive.read("clip.mp4") == b"z" * 10_000
            assert archive.getinfo("clip.mp4").compress_type == zipfile.ZIP_STORED

        stop.set()
        with zipfile.ZipFile(tmp_path / "cut.zip", "w") as archive, pytest.raises(md._Stopped):
            md._zip_add(archive, raw, "clip.mp4", stop, 0)

    def test_an_entry_that_would_eat_the_reserve_is_fatal(self, tmp_path):
        raw = tmp_path / "raw.part"
        raw.write_bytes(b"z" * 100)
        with zipfile.ZipFile(tmp_path / "a.zip", "w") as archive, pytest.raises(md._Fatal) as fatal:
            md._zip_add(archive, raw, "a.txt", threading.Event(), 2**62)
        assert fatal.value.code == md.ERROR_STORAGE


class TestReaderLease:
    def test_a_lease_that_never_started_lapses_and_a_started_one_holds_until_released(self, monkeypatch):
        runtime = md.MediaDownloadRuntime()
        job = md.DownloadJob("j", None, (), (), False, "f", None, 0, phase=md.PHASE_READY, artifact="x")
        pending = md.ReaderLease(runtime, job)
        job.leases.add(pending)
        assert job.pinned()
        pending.pending_until = time.monotonic() - 1
        assert not job.pinned()

        started = md.ReaderLease(runtime, job)
        job.leases.add(started)
        assert started.start() is True
        started.pending_until = time.monotonic() - 1
        assert job.pinned()
        assert started.start() is True  # already reading: asking again changes nothing
        started.release()
        started.release()  # idempotent
        assert not job.pinned() and started not in job.leases
        assert started.start() is False

    def test_a_promise_that_ran_out_cannot_be_started(self, tmp_path):
        # Codex review r1, D5: the janitor may have taken the file once the promise lapsed.
        runtime = md.MediaDownloadRuntime()
        job = md.DownloadJob("j", None, (), (), False, "f", tmp_path, 0, phase=md.PHASE_READY, artifact=tmp_path / "f")
        lease = runtime.lease(job)
        lease.pending_until = 0
        assert not job.pinned()
        assert lease.start() is False
        assert not job.pinned() and lease.released and lease not in job.leases

    @pytest.mark.parametrize("change", ["closed", "expired", "gone"])
    def test_a_read_does_not_start_once_the_result_is_no_longer_offered(self, tmp_path, change):
        runtime = md.MediaDownloadRuntime()
        job = md.DownloadJob("j", None, (), (), False, "f", tmp_path, 0, phase=md.PHASE_READY, artifact=tmp_path / "f")
        lease = runtime.lease(job)
        if change == "closed":
            runtime.close_admission()  # a restore
        elif change == "expired":
            job.phase = md.PHASE_EXPIRED
        else:
            job.artifact = None
        assert lease.start() is False
        assert not job.pinned()

    async def test_the_answer_refuses_before_its_headers(self, tmp_path):
        from fastapi import HTTPException

        from backend.app.services.live_media import WholeFileResponse

        artifact = tmp_path / "f"
        artifact.write_bytes(b"x")
        runtime = md.MediaDownloadRuntime()
        job = md.DownloadJob("j", None, (), (), False, "f", tmp_path, 0, phase=md.PHASE_READY, artifact=artifact)
        lease = runtime.lease(job)
        lease.pending_until = 0
        sent = []

        async def send(message):
            sent.append(message)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        scope = {"type": "http", "method": "GET", "headers": [], "path": "/"}
        with pytest.raises(HTTPException) as refused:
            await WholeFileResponse(artifact, lease=lease)(scope, receive, send)
        assert refused.value.status_code == 404 and sent == []


@pytest.mark.asyncio
class TestJoinOnCancel:
    """A cancelled caller resumes only after the lane's worker has really ended."""

    async def test_an_ftp_worker_is_joined(self):
        runtime = PrinterFileRuntime(global_workers=2)
        entered, release = threading.Event(), threading.Event()
        ended: list[float] = []

        def target(worker):
            entered.set()
            release.wait(5)
            ended.append(time.monotonic())
            return "done"

        for join in (True, False):
            entered.clear()
            release.clear()
            ended.clear()
            caller = asyncio.ensure_future(runtime.call("ftp:test", "fetch", target, join_on_cancel=join))
            await asyncio.to_thread(entered.wait, 5)
            caller.cancel()
            await asyncio.sleep(0.1)
            if join:
                assert not caller.done()  # still held: the thread is inside its work
                caller.cancel()  # a second cancel does not let it go either
                await asyncio.sleep(0.05)
                assert not caller.done()
            else:
                assert caller.cancelled()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await caller
            if join:
                assert ended, "the caller resumed before its worker ended"
            assert await runtime.quiesce(5)
            runtime.reopen()

    async def test_a_tunnel_task_is_joined(self):
        runtime = PrinterFileRuntime()
        cleaned = asyncio.Event()
        entered = asyncio.Event()

        async def operation():
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                # Cleanup that outlives the cancel, as a transfer's file removal does.
                for _ in range(5):
                    try:
                        await asyncio.sleep(0.02)
                    except asyncio.CancelledError:
                        continue
                cleaned.set()

        caller = asyncio.ensure_future(runtime.run_task("tunnel:test", "fetch", operation, join_on_cancel=True))
        await entered.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert cleaned.is_set()
        assert await runtime.quiesce(5)


class TestAccessLog:
    def test_the_grant_never_reaches_an_access_line(self):
        import logging

        from backend.app.core.logging_filters import DownloadGrantFilter

        record = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '%s - "%s %s HTTP/%s" %d',
            ("10.0.0.5:5123", "GET", "/api/v1/media-downloads/s3cr3t_Grant-Value/My%20files.zip", "1.1", 200),
            None,
        )
        assert DownloadGrantFilter().filter(record) is True
        line = record.getMessage()
        assert "s3cr3t_Grant-Value" not in line
        assert "/api/v1/media-downloads/[grant]/My%20files.zip" in line

        other = logging.LogRecord(
            "uvicorn.access", logging.INFO, __file__, 1, "%s %s %s", ("a", "GET", "/api/v1/x"), None
        )
        DownloadGrantFilter().filter(other)
        assert other.args == ("a", "GET", "/api/v1/x")


def _capture(account="operator", sid="s1", *, kind="session", key=None):
    from backend.app.core.auth_lifetime import FrozenAuthority

    return FrozenAuthority(kind, account, 1, sid=sid, key_identity=key)


def _scope(capture=None):
    from types import SimpleNamespace

    return md.JobScope("printer", 1, SimpleNamespace(capture=capture or _capture()))


@pytest.fixture
def runtime(tmp_path):
    made = md.MediaDownloadRuntime()
    made.root = tmp_path / "jobs"
    return made


def _held(runtime, monkeypatch):
    """Every job's preparation waits on the returned event."""
    release = asyncio.Event()

    async def prepare(_job):
        await release.wait()

    monkeypatch.setattr(runtime, "_prepare", prepare)
    return release


async def _create(runtime, capture=None, **extra):
    return await runtime.create(_scope(capture), [md.Source("file", None)], as_zip=False, filename=None, **extra)


@pytest.mark.asyncio
class TestAdmission:
    """Codex review r1, D2: the place is taken in the step that counted the places."""

    async def test_requests_arriving_together_see_each_other(self, runtime, monkeypatch):
        monkeypatch.setattr(md, "MAX_ACTIVE", 1)
        monkeypatch.setattr(md, "MAX_WAITING", 1)
        monkeypatch.setattr(md, "MAX_WAITING_PER_USER", 1)
        release = _held(runtime, monkeypatch)
        try:
            answers = await asyncio.gather(*(_create(runtime) for _ in range(3)), return_exceptions=True)
            admitted = [a for a in answers if isinstance(a, md.DownloadJob)]
            refused = [a for a in answers if isinstance(a, md.DownloadRefused)]
            assert (len(admitted), len(refused)) == (2, 1)
            assert refused[0].code in ("download_queue_full", "download_user_limit")
        finally:
            release.set()
            assert await runtime.quiesce(2)

    async def test_a_one_request_download_counts_toward_its_users_bound(self, runtime, monkeypatch):
        monkeypatch.setattr(md, "MAX_ACTIVE", 1)
        monkeypatch.setattr(md, "MAX_WAITING_PER_USER", 1)
        release = _held(runtime, monkeypatch)
        try:
            await _create(runtime, hidden=True)
            await asyncio.sleep(0)
            await _create(runtime, hidden=True)
            with pytest.raises(md.DownloadRefused) as refused:
                await _create(runtime, hidden=True)
            assert refused.value.code == "download_user_limit"
            # Another account is not held back by this one's waiting downloads.
            assert isinstance(await _create(runtime, _capture("other")), md.DownloadJob)
        finally:
            release.set()
            assert await runtime.quiesce(2)

    async def test_the_user_bound_counts_the_account_not_the_session(self, runtime, monkeypatch):
        monkeypatch.setattr(md, "MAX_ACTIVE", 1)
        monkeypatch.setattr(md, "MAX_WAITING_PER_USER", 1)
        release = _held(runtime, monkeypatch)
        try:
            await _create(runtime, _capture(sid="one"))
            await asyncio.sleep(0)
            await _create(runtime, _capture(sid="two"))
            with pytest.raises(md.DownloadRefused):
                await _create(runtime, _capture(sid="three"))
        finally:
            release.set()
            assert await runtime.quiesce(2)

    async def test_a_refused_admission_gives_its_place_back_and_leaves_nothing(self, runtime, monkeypatch):
        monkeypatch.setattr(md, "_base_reserve", lambda usage: usage.free + 1)
        with pytest.raises(md.DownloadRefused) as refused:
            await _create(runtime)
        assert refused.value.code == md.ERROR_STORAGE
        assert runtime.jobs == {} and list(runtime.root.iterdir()) == []

    async def test_an_admission_cancelled_on_its_disk_step_gives_its_place_back(self, runtime, monkeypatch):
        entered, release = threading.Event(), threading.Event()
        make = md._make_job_directory

        def slow(root, job_id):
            entered.set()
            release.wait(5)
            return make(root, job_id)

        monkeypatch.setattr(md, "_make_job_directory", slow)
        request = asyncio.create_task(_create(runtime))
        await asyncio.to_thread(entered.wait, 3)
        assert len(runtime.jobs) == 1  # the place is taken before the first disk call
        request.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert runtime.jobs == {} and list(runtime.root.iterdir()) == []

    async def test_a_restore_waits_for_an_admission_in_flight(self, runtime, monkeypatch):
        entered, release = threading.Event(), threading.Event()
        make = md._make_job_directory

        def slow(root, job_id):
            entered.set()
            release.wait(5)
            return make(root, job_id)

        monkeypatch.setattr(md, "_make_job_directory", slow)
        request = asyncio.create_task(_create(runtime))
        await asyncio.to_thread(entered.wait, 3)
        assert await runtime.quiesce(0.2) is False  # not proven: the admission is still on the disk
        release.set()
        with pytest.raises(md.DownloadRefused) as refused:
            await request
        assert refused.value.code == "downloads_unavailable"
        assert await runtime.quiesce(2) is True
        assert runtime.jobs == {} and list(runtime.root.iterdir()) == []

    async def test_the_lifetime_runs_from_creation_also_while_waiting(self, runtime, monkeypatch):
        monkeypatch.setattr(md, "MAX_ACTIVE", 1)
        release = _held(runtime, monkeypatch)
        try:
            first = await _create(runtime)
            await asyncio.sleep(0)
            monkeypatch.setattr(md, "JOB_LIFETIME_SECONDS", 0.05)
            waiting = await _create(runtime, _capture("other"))
            await asyncio.wait_for(runtime.wait(waiting), 3)
            # It never got a slot, and its time ran out all the same.
            assert (waiting.phase, waiting.error, waiting.started) == (md.PHASE_FAILED, md.ERROR_TIME, None)
            assert not waiting.directory.exists()
            assert not first.done.is_set()  # the one that holds the slot is untouched
        finally:
            release.set()
            assert await runtime.quiesce(2)


@pytest.mark.asyncio
class TestEarlyCancel:
    """Codex review r1, D3."""

    async def test_a_cancel_before_the_tasks_first_step_still_ends_the_job(self, runtime):
        job = await _create(runtime)
        runtime.request_cancel(job)
        await asyncio.gather(job.task, return_exceptions=True)
        assert (job.phase, job.done.is_set(), job.directory.exists()) == (md.PHASE_CANCELLED, True, False)
        # An ordinary waiter returns, and a second cancel is a no-op.
        await asyncio.wait_for(runtime.wait(job), 1)
        runtime.request_cancel(job)
        assert job.phase == md.PHASE_CANCELLED
        assert await runtime.quiesce(2)

    async def test_a_cancel_while_waiting_for_a_slot_ends_the_job(self, runtime, monkeypatch):
        monkeypatch.setattr(md, "MAX_ACTIVE", 1)
        release = _held(runtime, monkeypatch)
        try:
            await _create(runtime)
            waiting = await _create(runtime, _capture("other"))
            await asyncio.sleep(0.05)
            assert waiting.entered and waiting.phase == md.PHASE_QUEUED
            runtime.request_cancel(waiting)
            await asyncio.wait_for(runtime.wait(waiting), 2)
            assert (waiting.phase, waiting.directory.exists()) == (md.PHASE_CANCELLED, False)
        finally:
            release.set()
            assert await runtime.quiesce(2)


@pytest.mark.asyncio
class TestBudget:
    """Codex review r1, D4: the budget is of bytes taken from the source."""

    async def test_a_file_that_broke_off_is_charged_what_it_received(self, monkeypatch, tmp_path):
        from types import SimpleNamespace

        from backend.app.services.printer_files.base import TransferOutcome

        runtime = md.MediaDownloadRuntime()
        root = md._make_job_directory(tmp_path / "jobs", "job")
        monkeypatch.setattr(md, "BYTE_BUDGET", 10)
        budgets = []

        async def short(path, *, byte_budget, **_kw):
            budgets.append(byte_budget)
            return TransferOutcome(md.TRANSFER_UNAVAILABLE, 8, reason="interrupted")

        async def small(path, *, byte_budget, **_kw):
            budgets.append(byte_budget)
            path.write_bytes(b"12")
            return TransferOutcome("completed", 2, "size_verified")

        async def never(path, *, byte_budget, **_kw):
            budgets.append(byte_budget)
            raise AssertionError("nothing is left for this file")

        sources = (md.Source("first", short), md.Source("second", small), md.Source("third", never))
        job = md.DownloadJob(
            "job",
            _scope(),
            sources,
            ("first", "second", "third"),
            True,
            "files.zip",
            root,
            0,
            results=[{"name": s.name, "status": md.FILE_PENDING} for s in sources],
        )
        with pytest.raises(md._Fatal) as fatal:
            await runtime._fetch_all(job, SimpleNamespace(deliverable=lambda: True))
        assert fatal.value.code == md.ERROR_LIMIT
        assert budgets == [10, 2]


@pytest.mark.asyncio
class TestLocalCopy:
    """Codex review r1, D4 and D7: whose failure it is, and which file was opened."""

    async def _fetch(self, source, destination, budget=10**9):
        return await source.fetch(
            destination, byte_budget=budget, reserve_bytes=0, progress=None, stop=threading.Event()
        )

    async def test_a_scratch_that_cannot_be_written_ends_the_whole_job(self, tmp_path):
        video = tmp_path / "video.mp4"
        video.write_bytes(b"v" * 10)
        outcome = await self._fetch(md.local_source(video, name="v.mp4"), tmp_path / "no-such-dir" / "0.part")
        assert outcome.status == md.TRANSFER_INSUFFICIENT_STORAGE

    async def test_a_full_disk_while_copying_ends_the_whole_job(self, tmp_path, monkeypatch):
        import errno
        import io

        video = tmp_path / "video.mp4"
        video.write_bytes(b"v" * 10)
        destination = tmp_path / "0.part"

        class Full(io.FileIO):
            def write(self, _data):
                raise OSError(errno.ENOSPC, "No space left on device")

        real_open = Path.open

        def opening(self, mode="r", *args, **kwargs):
            if self == destination and "w" in mode:
                return Full(str(self), "w")
            return real_open(self, mode, *args, **kwargs)

        monkeypatch.setattr(Path, "open", opening)
        outcome = await self._fetch(md.local_source(video, name="v.mp4"), destination)
        assert outcome.status == md.TRANSFER_INSUFFICIENT_STORAGE
        assert not destination.exists()

    async def test_a_missing_source_is_that_files_own_failure(self, tmp_path):
        outcome = await self._fetch(md.local_source(tmp_path / "gone.mp4", name="v.mp4"), tmp_path / "0.part")
        assert (outcome.status, outcome.reason) == ("not_readable", "missing")

    async def test_a_file_put_in_the_chosen_ones_name_is_not_copied(self, tmp_path):
        video = tmp_path / "video.mp4"
        video.write_bytes(b"first")
        identity = md.file_identity(video.stat())
        replacement = tmp_path / "new.part"
        replacement.write_bytes(b"other bytes")
        replacement.replace(video)
        destination = tmp_path / "0.part"
        outcome = await self._fetch(md.local_source(video, name="v.mp4", identity=identity), destination)
        assert (outcome.status, outcome.reason) == ("not_readable", "replaced")
        assert not destination.exists()

    async def test_the_same_file_is_copied(self, tmp_path):
        video = tmp_path / "video.mp4"
        video.write_bytes(b"first")
        destination = tmp_path / "0.part"
        source = md.local_source(video, name="v.mp4", identity=md.file_identity(video.stat()))
        outcome = await self._fetch(source, destination)
        assert outcome.completed and destination.read_bytes() == b"first"

    async def test_a_video_no_longer_shown_is_left_out_before_and_after_the_copy(self, tmp_path):
        video = tmp_path / "video.mp4"
        video.write_bytes(b"first")
        answers = iter([False])

        async def shown():
            return next(answers)

        destination = tmp_path / "0.part"
        outcome = await self._fetch(md.local_source(video, name="v.mp4", still_shown=shown), destination)
        assert (outcome.status, outcome.reason, destination.exists()) == ("not_readable", "replaced", False)

        answers = iter([True, False])  # replaced while it was being copied
        outcome = await self._fetch(md.local_source(video, name="v.mp4", still_shown=shown), destination)
        assert (outcome.status, outcome.reason, outcome.received) == ("not_readable", "replaced", 5)
        assert not destination.exists()


@pytest.mark.asyncio
class TestRestoreProof:
    """Codex review r1, D5: a restore is told about what could not be cleaned."""

    async def test_a_directory_that_could_not_be_removed_refuses_the_restore(self, tmp_path):
        from unittest.mock import AsyncMock

        runtime = md.MediaDownloadRuntime()
        directory = md._make_job_directory(tmp_path / "jobs", "one")
        job = md.DownloadJob("one", _scope(), (), (), False, "x", directory, 0, phase=md.PHASE_READY)
        runtime.jobs[job.job_id] = job
        remove = runtime._remove
        runtime._remove = AsyncMock(return_value=False)
        assert await runtime.quiesce(0.1) is False
        assert job.job_id in runtime.jobs  # the debt is kept, not forgotten
        runtime._remove = remove
        assert await runtime.quiesce(1) is True
        assert runtime.jobs == {} and not directory.exists()

    async def test_a_removal_still_running_refuses_the_restore(self, tmp_path):
        runtime = md.MediaDownloadRuntime()
        directory = md._make_job_directory(tmp_path / "jobs", "one")
        job = md.DownloadJob("one", _scope(), (), (), False, "x", directory, 0, phase=md.PHASE_READY)
        runtime.jobs[job.job_id] = job
        release = asyncio.Event()

        async def slow(_directory):
            await release.wait()
            return True

        runtime._remove = slow
        runtime.discard_soon(job, forget=True)
        assert await runtime.quiesce(0.1) is False
        release.set()
        assert await runtime.quiesce(1) is True

    async def test_a_reader_that_started_is_waited_for_and_a_pending_one_is_refused(self, tmp_path):
        runtime = md.MediaDownloadRuntime()
        directory = md._make_job_directory(tmp_path / "jobs", "one")
        artifact = directory / "artifact"
        artifact.write_bytes(b"x")
        job = md.DownloadJob("one", _scope(), (), (), False, "x", directory, 0, phase=md.PHASE_READY, artifact=artifact)
        runtime.jobs[job.job_id] = job
        reading, pending = runtime.lease(job), runtime.lease(job)
        assert reading.start() is True
        assert await runtime.quiesce(0.1) is False  # somebody is reading it
        assert directory.exists()
        assert pending.start() is False  # promised, never started: refused once the restore began
        reading.release()
        assert await runtime.quiesce(1) is True
        assert not directory.exists()


class TestGrantInLogs:
    """Codex review r1, D6: the path of a native download is its credential, on every output."""

    MARKER = "review_marker_only"

    def _captured(self, logger):
        import logging
        from io import StringIO

        from backend.app.core.logging_filters import CredentialSafeFormatter

        sink = StringIO()
        handler = logging.StreamHandler(sink)
        handler.setFormatter(CredentialSafeFormatter("%(message)s"))
        logger.addHandler(handler)
        return sink, handler

    @pytest.mark.parametrize("method", ["GET", "HEAD"])
    def test_a_slow_request_line_carries_no_grant(self, monkeypatch, method):
        from backend.app.core import query_timing

        sink, handler = self._captured(query_timing.logger)
        monkeypatch.setattr(query_timing, "_request_threshold_ms", 1)
        records = []
        monkeypatch.setattr(
            query_timing.logger, "handle", lambda record: (records.append(record), handler.handle(record))
        )
        try:
            path = f"/api/v1/media-downloads/{self.MARKER}/sample.zip"
            assert query_timing.log_slow_request(method, path, 2, query_timing.RequestTiming(), "test")
        finally:
            query_timing.logger.removeHandler(handler)
        assert self.MARKER not in sink.getvalue()
        assert "/api/v1/media-downloads/[grant]/sample.zip" in sink.getvalue()
        # Masked in the record itself — safe on a handler with any formatter.
        assert all(self.MARKER not in record.getMessage() for record in records)

    def test_any_line_through_our_formatter_is_masked(self):
        from backend.app.core.logging_filters import mask_download_grants, redact_log_credentials

        line = f"refused GET /api/v1/media-downloads/{self.MARKER}/My%20files.zip (401)"
        assert self.MARKER not in redact_log_credentials(line)
        assert mask_download_grants("GET /api/v1/archives/1/media-download-jobs") == (
            "GET /api/v1/archives/1/media-download-jobs"
        )

    def test_the_access_logger_masks_with_or_without_a_log_file(self):
        import logging

        import backend.app.main  # noqa: F401 — installs the filter at import
        from backend.app.core.logging_filters import DownloadGrantFilter

        assert any(isinstance(f, DownloadGrantFilter) for f in logging.getLogger("uvicorn.access").filters)
        source = Path(backend.app.main.__file__).read_text(encoding="utf-8")
        installed = source.index("addFilter(DownloadGrantFilter())")
        line_start = source.rfind("\n", 0, installed) + 1
        # At module level: not inside ``if app_settings.log_to_file``.
        assert source[line_start] != " "


@pytest.mark.asyncio
class TestAuthorityDeadline:
    """The coordinator's real one-second deadline (the integration tests run with a patient one)."""

    async def test_authority_that_cannot_be_read_stops_the_preparation(self, monkeypatch, tmp_path):
        from backend.app.core.auth_lifetime import FrozenAuthority
        from backend.app.services import live_authority as la

        runtime = md.MediaDownloadRuntime()
        runtime.root = tmp_path / "jobs"
        monitor = la.LiveAuthority()
        monkeypatch.setattr(md, "live_authority", monitor)
        entered = asyncio.Event()

        async def slow(_batch):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(monitor, "_read", slow)
        assert la.CHECK_SECONDS == 1.0
        descriptor = la.LiveDescriptor(FrozenAuthority("session", "review", 1), ("printers:files",))
        job = await runtime.create(
            md.JobScope("printer", 1, descriptor), [md.Source("file", None)], as_zip=False, filename=None
        )
        try:
            await asyncio.wait_for(entered.wait(), 3)
            await asyncio.wait_for(job.done.wait(), 5)
            assert (job.phase, job.error, job.artifact) == (md.PHASE_FAILED, md.ERROR_AUTHORITY, None)
            assert not job.directory.exists()
        finally:
            await runtime.quiesce(2)
            await monitor.shutdown(2)


@pytest.mark.asyncio
class TestSlotsAreSpokenForOnce:
    """Codex re-acceptance, D2-R: a free slot another account's job is about to take is not everybody's room."""

    async def test_an_account_waits_with_at_most_its_own_bound(self, runtime, monkeypatch):
        release = _held(runtime, monkeypatch)
        assert (md.MAX_ACTIVE, md.MAX_WAITING_PER_USER) == (2, 2)  # the shipped bounds
        try:
            # All five arrive before any of them has a slot: the first two take both.
            answers = await asyncio.gather(
                *(_create(runtime, _capture(account)) for account in ("A", "A", "B", "B", "B")),
                return_exceptions=True,
            )
            assert [type(a).__name__ for a in answers] == ["DownloadJob"] * 4 + ["DownloadRefused"]
            assert answers[4].code == "download_user_limit" and answers[4].status == 429
            await asyncio.sleep(0.05)
            waiting = [j for j in runtime.jobs.values() if j.waiting and j.capture.account_identity == "B"]
            assert len(waiting) == md.MAX_WAITING_PER_USER
        finally:
            release.set()
            assert await runtime.quiesce(2)

    async def test_a_free_slot_is_still_not_refused(self, runtime, monkeypatch):
        release = _held(runtime, monkeypatch)
        try:
            # One account, four at once: two run, two wait, the fifth is over its bound.
            answers = await asyncio.gather(*(_create(runtime) for _ in range(5)), return_exceptions=True)
            assert [type(a).__name__ for a in answers] == ["DownloadJob"] * 4 + ["DownloadRefused"]
        finally:
            release.set()
            assert await runtime.quiesce(2)

    async def test_the_global_bound_counts_what_really_waits(self, runtime, monkeypatch):
        monkeypatch.setattr(md, "MAX_WAITING", 2)
        release = _held(runtime, monkeypatch)
        try:
            accounts = ("A", "B", "C", "D", "E")  # two run, two wait, the fifth finds the queue full
            answers = await asyncio.gather(*(_create(runtime, _capture(a)) for a in accounts), return_exceptions=True)
            assert [type(a).__name__ for a in answers] == ["DownloadJob"] * 4 + ["DownloadRefused"]
            assert answers[4].code == "download_queue_full"
        finally:
            release.set()
            assert await runtime.quiesce(2)


def _old_failed_job(runtime, tmp_path):
    directory = md._make_job_directory(tmp_path / "scratch", "one")
    (directory / "partial.bin").write_bytes(b"sample")
    job = md.DownloadJob("one", _scope(), (), (), False, "one", directory, 0, phase=md.PHASE_FAILED)
    job.error = md.ERROR_CLEANUP
    job.finished = time.monotonic() - md.TERMINAL_TTL_SECONDS - 5
    runtime.jobs[job.job_id] = job
    return job


@pytest.mark.asyncio
class TestCleanupDebt:
    """Codex re-acceptance, D5-R: what is still on disk is still ours to answer for."""

    async def test_the_janitor_does_not_forget_a_directory_it_could_not_remove(self, tmp_path):
        from unittest.mock import AsyncMock

        runtime = md.MediaDownloadRuntime()
        job = _old_failed_job(runtime, tmp_path)
        remove = runtime._remove
        runtime._remove = AsyncMock(return_value=False)
        await runtime.sweep()
        assert job.job_id in runtime.jobs
        assert await runtime.quiesce(0.05) is False
        assert (job.directory / "partial.bin").exists()
        # Once the disk lets go, the next sweep forgets it and a drain is proven.
        runtime._remove = remove
        runtime.closed = False
        await runtime.sweep()
        assert runtime.jobs == {} and not job.directory.exists()
        assert await runtime.quiesce(1) is True

    async def test_a_restore_answers_at_its_deadline_and_the_removal_goes_on(self, tmp_path):
        runtime = md.MediaDownloadRuntime()
        job = _old_failed_job(runtime, tmp_path)
        entered, finish = asyncio.Event(), asyncio.Event()
        remove = runtime._remove

        async def slow(directory):
            entered.set()
            await finish.wait()
            return await remove(directory)

        runtime._remove = slow
        started = time.monotonic()
        assert await runtime.quiesce(0.05) is False
        assert time.monotonic() - started < 0.5 and entered.is_set()
        assert job.job_id in runtime.jobs and job.directory.exists()  # still owned, still being removed
        # The removal that was begun is the one that finishes — no second one is started beside it.
        entered.clear()
        assert await runtime.quiesce(0.05) is False and not entered.is_set()
        finish.set()
        assert await runtime.quiesce(2) is True
        assert runtime.jobs == {} and not job.directory.exists()

    async def test_a_removal_that_failed_is_tried_again_by_the_next_drain(self, tmp_path):
        runtime = md.MediaDownloadRuntime()
        job = _old_failed_job(runtime, tmp_path)
        answers = iter([False, True])
        remove = runtime._remove

        async def flaky(directory):
            return next(answers) and await remove(directory)

        runtime._remove = flaky
        assert await runtime.quiesce(0.5) is False
        assert await runtime.quiesce(0.5) is True
        assert not job.directory.exists()


@pytest.mark.asyncio
class TestTheScratchRefuses:
    """Codex re-acceptance, D4-R: this server's disk refusing is the job's failure on every transport."""

    def _full(self, *_args, **_kwargs):
        import errno

        raise OSError(errno.ENOSPC, "No space left on device")

    async def _job_with_second_source(self, tmp_path, fetch):
        from types import SimpleNamespace

        from backend.app.services.printer_files.base import TransferOutcome

        runtime = md.MediaDownloadRuntime()
        directory = md._make_job_directory(tmp_path, "demo")

        async def good(path, **_kwargs):
            path.write_bytes(b"item")
            return TransferOutcome("completed", 4, "size_verified")

        sources = (md.Source("one", good), md.Source("two", fetch))
        job = md.DownloadJob(
            "demo",
            md.JobScope("printer", 1, SimpleNamespace()),
            sources,
            ("one", "two"),
            True,
            "result.zip",
            directory,
            0,
            results=[{"name": s.name, "status": md.FILE_PENDING} for s in sources],
        )
        with pytest.raises(md._Fatal) as fatal:
            await runtime._fetch_all(job, SimpleNamespace(deliverable=lambda: True))
        return fatal.value.code

    async def test_ftp_a_scratch_that_cannot_be_opened_ends_the_job(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        from backend.app.services import bambu_ftp
        from backend.app.services.printer_files.base import TransferOutcome

        monkeypatch.setattr(bambu_ftp, "open", self._full, raising=False)

        async def fetch(destination, **_kwargs):
            answer = bambu_ftp.BambuFTPClient.fetch_to_file(SimpleNamespace(_ftp=True), "/two", destination)
            return TransferOutcome(*answer)

        assert await self._job_with_second_source(tmp_path, fetch) == md.ERROR_STORAGE

    async def test_ftp_a_write_the_disk_refuses_ends_the_job_and_a_dead_socket_does_not(self, tmp_path):
        import errno
        from types import SimpleNamespace

        from backend.app.services import bambu_ftp

        class Full:
            def retrbinary(self, _command, write, blocksize=0):
                write(b"x" * 10)

        class Dead:
            def retrbinary(self, _command, write, blocksize=0):
                raise ConnectionResetError(errno.ECONNRESET, "reset")

        client = SimpleNamespace(_ftp=Full(), get_file_size=lambda _path: 10)
        destination = tmp_path / "raw" / "0.part"
        real_open = open

        class Refusing:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.handle.close()

            def write(self, _chunk):
                raise OSError(errno.ENOSPC, "No space left on device")

            def __getattr__(self, name):
                return getattr(self.handle, name)

        bambu_ftp.open = lambda path, mode: Refusing(real_open(path, mode))
        try:
            status, _received, _check, reason = bambu_ftp.BambuFTPClient.fetch_to_file(client, "/a", destination)
        finally:
            del bambu_ftp.open
        assert (status, reason) == ("insufficient_storage", "local_OSError")
        assert not destination.exists()

        client._ftp = Dead()
        status, _received, _check, reason = bambu_ftp.BambuFTPClient.fetch_to_file(client, "/a", destination)
        assert (status, reason) == ("unavailable", "ConnectionResetError")  # the printer's side: one file's failure

    async def test_tunnel_a_scratch_that_cannot_be_opened_ends_the_job(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        from backend.app.services.printer_files import tunnel

        monkeypatch.setattr(tunnel, "open", self._full, raising=False)
        client = tunnel.TunnelTransport(SimpleNamespace(ip_address="127.0.0.1", access_code="test"))

        async def lane(_name, operation, **_kwargs):
            return await operation()

        monkeypatch.setattr(client, "_on_lane", lane)

        async def fetch(destination, **_kwargs):
            return await client.download_to_file("/two", destination)

        assert await self._job_with_second_source(tmp_path, fetch) == md.ERROR_STORAGE
