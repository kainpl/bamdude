"""The download job service's own pieces, and the lane rule it leans on (G10 D)."""

from __future__ import annotations

import asyncio
import threading
import time
import zipfile

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
        started.start()
        started.pending_until = time.monotonic() - 1
        assert job.pinned()
        started.release()
        started.release()  # idempotent
        assert not job.pinned() and started not in job.leases


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
