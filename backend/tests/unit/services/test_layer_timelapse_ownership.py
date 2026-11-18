"""Who owns a layer recording's frames, and what happens to them (G10 F, M27–M29).

The stitches here are real: ffmpeg over JPEG frames written by Pillow, in the
test's own temporary directory. Nothing touches a shared location.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from backend.app.services import layer_timelapse as lt, media_child, timelapse_processor

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg / ffprobe are not installed")


@pytest.fixture(autouse=True)
def frames(tmp_path, monkeypatch):
    """The frames tree in the test's own directory, and a clean module state before and after."""
    monkeypatch.setattr(lt.settings, "base_dir", tmp_path)
    monkeypatch.setattr(timelapse_processor, "get_ffmpeg_path", lambda: FFMPEG)
    lt._active_sessions.clear()
    lt._finalizing.clear()
    lt.reopen()
    yield tmp_path / lt.FRAMES_DIRNAME
    lt._active_sessions.clear()
    lt._finalizing.clear()
    lt.reopen()
    deadline = time.monotonic() + 15
    while media_child.live() and time.monotonic() < deadline:
        time.sleep(0.05)


def _session(printer_id: int = 1, archive_id: int | None = 100) -> lt.TimelapseSession:
    return lt.start_session(printer_id, archive_id, "http://camera/", "mjpeg")


def _jpeg(size: tuple[int, int], shade: int) -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, (shade, 255 - shade, 128)).save(buffer, "JPEG", quality=70)
    return buffer.getvalue()


def _fill(session: lt.TimelapseSession, count: int = 4, size: tuple[int, int] = (320, 240)) -> None:
    for layer in range(1, count + 1):
        data = _jpeg(size, (layer * 40) % 255)
        (session.frames_dir / f"layer_{layer:05d}.jpg").write_bytes(data)
        session.frame_count += 1
        session.bytes_written += len(data)


def _manifest(directory: Path) -> dict:
    return json.loads((directory / lt.MANIFEST).read_text(encoding="utf-8"))


def _dimensions(path: Path) -> tuple[int, int]:
    out = subprocess.run(
        [FFPROBE, "-v", "quiet", "-print_format", "json", "-show_streams", str(path)],
        check=True,
        capture_output=True,
        timeout=60,
    ).stdout
    stream = next(s for s in json.loads(out)["streams"] if s["codec_type"] == "video")
    return stream["width"], stream["height"]


def _retained(root: Path, printer: int, name: str, *, age_hours: float, size: int = 10, reason="stitch_failed") -> Path:
    """A session directory an earlier run left ``retained``."""
    directory = root / str(printer) / name
    directory.mkdir(parents=True)
    (directory / "layer_00001.jpg").write_bytes(b"x" * size)
    lt._write_manifest(
        directory,
        {
            "kind": lt.MANIFEST_KIND,
            "session_id": name,
            "state": lt.STATE_RETAINED,
            "reason": reason,
            "retained_at": time.time() - age_hours * 3600,
        },
    )
    return directory


class TestTheDirectorySaysWhoseItIs:
    def test_a_new_session_writes_its_manifest(self, frames):
        session = _session(printer_id=7, archive_id=55)
        manifest = _manifest(session.frames_dir)
        assert manifest["kind"] == lt.MANIFEST_KIND and manifest["state"] == lt.STATE_ACTIVE
        assert (manifest["session_id"], manifest["printer_id"], manifest["archive_id"]) == (session.session_id, 7, 55)
        assert session.frames_dir == frames / "7" / session.session_id

    def test_a_new_session_of_a_printer_removes_only_its_predecessor(self, frames):
        first = _session(printer_id=3)
        _fill(first, 2)
        second = _session(printer_id=3)
        assert not first.frames_dir.exists() and second.frames_dir.is_dir()
        assert lt.get_session(3) is second


class TestNothingIsRemovedUnderAWriterOrAReader:
    def test_cleanup_waits_for_the_last_holder(self):
        session = _session()
        _fill(session, 2)
        session._hold()  # a frame is being written, or the encoder reads
        session._hold()
        session.cleanup()
        assert session.frames_dir.is_dir()
        session._release()
        assert session.frames_dir.is_dir()
        session._release()
        assert not session.frames_dir.exists()

    @pytest.mark.asyncio
    async def test_a_frame_that_arrives_after_the_cancel_is_not_written(self, monkeypatch):
        """The camera answers slowly; the print is cancelled meanwhile."""
        from backend.app.services.camera_metrics import CameraCaptureResult

        session = _session()
        answered = asyncio.Event()

        async def slow_capture(_request):
            await answered.wait()
            return CameraCaptureResult(_jpeg((64, 48), 10), "fresh")

        monkeypatch.setattr(lt, "capture", slow_capture)
        monkeypatch.setattr("backend.app.api.routes.camera.live_frame_for_capture", lambda _pid: (False, None))
        capturing = asyncio.create_task(session.capture_layer(1))
        await asyncio.sleep(0.05)
        lt.cancel_session(1)
        assert session.frames_dir.is_dir()  # held by the capture in flight
        answered.set()
        assert await capturing is False
        assert session.frame_count == 0
        assert not session.frames_dir.exists()  # removed once the capture had ended

    @pytest.mark.asyncio
    async def test_a_recorder_that_is_closed_for_a_restore_writes_nothing(self, monkeypatch):
        session = _session()
        calls = []

        async def capture(_request):
            calls.append(1)

        monkeypatch.setattr(lt, "capture", capture)
        assert await lt.quiesce(1) is True
        assert await session.capture_layer(1) is False
        assert calls == []
        assert lt.start_session(2, None, "http://camera/", "mjpeg") is None
        lt.reopen()
        assert lt.admitting()


class TestCaptureIsBounded:
    @pytest.mark.asyncio
    async def test_capture_stops_at_the_frame_storage_limit_and_says_so_once(self, monkeypatch, caplog):
        from backend.app.services.camera_metrics import CameraCaptureResult

        frame = _jpeg((64, 48), 20)

        async def capture(_request):
            return CameraCaptureResult(frame, "fresh")

        monkeypatch.setattr(lt, "capture", capture)
        monkeypatch.setattr("backend.app.api.routes.camera.live_frame_for_capture", lambda _pid: (False, None))
        monkeypatch.setattr(lt, "ACTIVE_BYTES", len(frame) * 2)
        monkeypatch.setattr(lt, "RESERVE_FLOOR", 0)
        monkeypatch.setattr(lt, "RESERVE_SHARE", 0.0)
        session = _session()
        with caplog.at_level("WARNING", logger="backend.app.services.layer_timelapse"):
            results = [await session.capture_layer(layer) for layer in range(1, 6)]
        assert results == [True, True, False, False, False]
        assert session.frame_count == 2
        warnings = [r for r in caplog.records if "stopped capturing" in r.getMessage()]
        assert len(warnings) == 1  # said once, not once per layer


class TestStitch:
    def test_the_time_limit_grows_with_the_frames_between_its_bounds(self):
        assert lt.stitch_limit(0) == lt.STITCH_MIN_SECONDS
        assert lt.stitch_limit(1000) == lt.STITCH_MIN_SECONDS + 500
        assert lt.stitch_limit(10**7) == lt.STITCH_MAX_SECONDS

    @needs_ffmpeg
    @pytest.mark.asyncio
    @pytest.mark.parametrize("size", [(3840, 2160), (1080, 1920)], ids=["4k", "portrait"])
    async def test_a_real_stitch_keeps_the_resolution_and_bounds_the_encoder(self, monkeypatch, size):
        commands: list[list[str]] = []
        real = media_child.run

        async def recording(argv, **kwargs):
            commands.append(argv)
            return await real(argv, **kwargs)

        monkeypatch.setattr(lt.media_child, "run", recording)
        session = lt.take_session(1, _session())
        _fill(session, 5, size)
        video = await lt.finish_session(session, keep_frames=True)
        assert video is not None and video.is_file()
        assert _dimensions(video) == size  # no downscale (OD-6)
        stitch = next(argv for argv in commands if "concat" in argv)
        assert stitch[stitch.index("-threads") + 1] == str(lt.ENCODER_THREADS)
        assert f"rc-lookahead={lt.ENCODER_LOOKAHEAD}" in stitch
        assert not any("scale=" in part for part in stitch)
        # Kept until the caller says the video was published.
        assert session.frames_dir.is_dir() and _manifest(session.frames_dir)["state"] == lt.STATE_FINALIZING
        session.cleanup()
        assert not session.frames_dir.exists()
        assert media_child.live() == 0

    @needs_ffmpeg
    @pytest.mark.asyncio
    async def test_without_keep_frames_a_successful_stitch_removes_them(self):
        session = lt.take_session(1, _session())
        _fill(session, 3)
        video = await lt.finish_session(session)
        assert video is not None and video.is_file()
        assert not session.frames_dir.exists()

    @needs_ffmpeg
    @pytest.mark.asyncio
    async def test_a_failed_stitch_keeps_the_frames_and_removes_its_own_output(self):
        session = lt.take_session(1, _session())
        for layer in (1, 2, 3):
            (session.frames_dir / f"layer_{layer:05d}.jpg").write_bytes(b"this is not a jpeg")
            session.frame_count += 1
        assert await lt.finish_session(session, keep_frames=True) is None
        manifest = _manifest(session.frames_dir)
        assert manifest["state"] == lt.STATE_RETAINED and manifest["reason"].startswith("stitch_")
        assert len(list(session.frames_dir.glob("layer_*.jpg"))) == 3
        assert not list(session.frames_dir.parent.glob("timelapse_*.mp4"))
        assert lt._finalizing == {}

    @pytest.mark.asyncio
    async def test_a_stopped_stitch_kills_its_encoder_and_keeps_the_frames(self, monkeypatch):
        """What a restore does to a stitch in flight. The encoder is a stand-in that never finishes."""
        monkeypatch.setattr(lt, "get_ffmpeg_path", lambda: sys.executable)
        real = media_child.run

        async def hanging(_argv, **kwargs):
            return await real([sys.executable, "-c", "import time; time.sleep(120)"], **kwargs)

        monkeypatch.setattr(lt.media_child, "run", hanging)
        session = lt.take_session(1, _session())
        _fill(session, 3)
        finishing = asyncio.create_task(lt.finish_session(session, keep_frames=True))
        while media_child.live() == 0:
            await asyncio.sleep(0.05)
        assert await lt.quiesce(15) is True  # it returns only once the encoder has really ended
        assert media_child.live() == 0
        assert await finishing is None
        assert _manifest(session.frames_dir)["reason"] == "stitch_cancelled"
        assert len(list(session.frames_dir.glob("layer_*.jpg"))) == 3

    @pytest.mark.asyncio
    async def test_an_unreaped_encoder_pins_its_frames_against_every_cleanup(self, monkeypatch):
        monkeypatch.setattr(lt, "get_ffmpeg_path", lambda: sys.executable)

        async def unreaped(_argv, **_kwargs):
            return media_child.ChildResult(media_child.UNREAPED, reason="not reaped after timeout")

        monkeypatch.setattr(lt.media_child, "run", unreaped)
        session = lt.take_session(1, _session())
        _fill(session, 2)
        output = session.frames_dir.parent / f"timelapse_{session.session_id}.mp4"
        output.write_bytes(b"half a video")
        assert await lt.finish_session(session, keep_frames=True) is None
        session.cleanup()  # whoever asks now
        assert session.frames_dir.is_dir()  # the encoder may still be reading them
        assert output.is_file()  # and writing this

    @needs_ffmpeg
    @pytest.mark.asyncio
    async def test_a_finaliser_does_not_touch_the_next_recording_of_the_same_printer(self):
        first = _session(printer_id=4)
        _fill(first, 3)
        finishing = lt.take_session(4, first)
        second = _session(printer_id=4)  # the next print started while the first is stitched
        assert first.frames_dir.is_dir()  # detached: the new session did not cancel it
        _fill(second, 2)
        video = await lt.finish_session(finishing)
        assert video is not None
        assert second.frames_dir.is_dir() and len(list(second.frames_dir.glob("layer_*.jpg"))) == 2
        assert lt.get_session(4) is second and _manifest(second.frames_dir)["state"] == lt.STATE_ACTIVE

    @pytest.mark.asyncio
    async def test_a_refused_publication_keeps_the_frames(self):
        session = lt.take_session(1, _session())
        _fill(session, 2)
        session.state = lt.STATE_FINALIZING
        session.retain("not_published_source_changed")
        manifest = _manifest(session.frames_dir)
        assert (manifest["state"], manifest["reason"]) == (lt.STATE_RETAINED, "not_published_source_changed")
        assert session.frames_dir.is_dir()

    def test_retain_never_undoes_a_cleanup(self):
        session = _session()
        session.cleanup()
        session.retain("late")
        assert not session.frames_dir.exists()


class TestJanitor:
    def test_an_interrupted_session_becomes_retained_and_stays(self, frames):
        directory = frames / "1" / ("a" * 32)
        directory.mkdir(parents=True)
        (directory / "layer_00001.jpg").write_bytes(b"x")
        lt._write_manifest(
            directory, {"kind": lt.MANIFEST_KIND, "session_id": "a" * 32, "state": lt.STATE_ACTIVE, "printer_id": 1}
        )
        assert lt.sweep_orphaned_frames() == 0
        manifest = _manifest(directory)
        assert (manifest["state"], manifest["reason"]) == (lt.STATE_RETAINED, "interrupted")
        assert (directory / "layer_00001.jpg").is_file()

    def test_a_live_session_is_never_settled_or_removed(self, frames, monkeypatch):
        monkeypatch.setattr(lt, "RETAIN_BYTES", 0)
        session = _session(printer_id=2)
        _fill(session, 2)
        assert lt.sweep_orphaned_frames() == 0
        assert _manifest(session.frames_dir)["state"] == lt.STATE_ACTIVE
        assert len(list(session.frames_dir.glob("layer_*.jpg"))) == 2

    def test_retained_frames_older_than_the_bound_go(self, frames):
        old = _retained(frames, 1, "b" * 32, age_hours=25)
        fresh = _retained(frames, 1, "c" * 32, age_hours=23)
        assert lt.sweep_orphaned_frames() == 1
        assert not old.exists() and fresh.is_dir()

    def test_over_the_byte_bound_the_oldest_go_first(self, frames, monkeypatch):
        monkeypatch.setattr(lt, "RETAIN_BYTES", 2500)
        oldest = _retained(frames, 1, "d" * 32, age_hours=5, size=1000)
        middle = _retained(frames, 2, "e" * 32, age_hours=3, size=1000)
        newest = _retained(frames, 3, "f" * 32, age_hours=1, size=1000)
        assert lt.apply_retention() >= 1
        assert not oldest.exists() and middle.is_dir() and newest.is_dir()

    def test_what_is_not_provably_ours_is_left_alone_and_never_entered(self, frames):
        # No manifest and not the old shape; a manifest that names another session; a plain file.
        foreign = frames / "1" / "somebody-elses"
        foreign.mkdir(parents=True)
        (foreign / "important.txt").write_text("keep", encoding="utf-8")
        liar = frames / "1" / ("9" * 32)
        liar.mkdir()
        (liar / lt.MANIFEST).write_text(
            json.dumps({"kind": lt.MANIFEST_KIND, "session_id": "another", "state": "retained", "retained_at": 0}),
            encoding="utf-8",
        )
        (frames / "stray.bin").write_bytes(b"x")
        mixed = frames / "1" / "20260101_010101"  # the old name, but not only frames inside
        mixed.mkdir()
        (mixed / "layer_00001.jpg").write_bytes(b"x")
        (mixed / "notes.txt").write_text("mine", encoding="utf-8")
        os.utime(mixed, (0, 0))
        assert lt.sweep_orphaned_frames() == 0
        assert (foreign / "important.txt").is_file() and liar.is_dir() and (mixed / "notes.txt").is_file()
        assert (frames / "stray.bin").is_file()

    def test_a_directory_of_the_old_shape_is_adopted_and_then_bounded(self, frames):
        stale = frames / "1" / "20260101_010101"
        stale.mkdir(parents=True)
        (stale / "layer_00001.jpg").write_bytes(b"x")
        (stale / "frames.txt").write_text("file 'layer_00001.jpg'\n", encoding="utf-8")
        long_ago = time.time() - 48 * 3600
        os.utime(stale, (long_ago, long_ago))
        recent = frames / "2" / "20260102_020202"
        recent.mkdir(parents=True)
        (recent / "layer_00001.jpg").write_bytes(b"x")
        assert lt.sweep_orphaned_frames() == 1
        assert not stale.exists()
        assert recent.is_dir() and _manifest(recent)["reason"] == "legacy"

    def test_a_dead_stitchs_output_goes_and_an_emptied_printer_directory_is_dropped(self, frames):
        _retained(frames, 9, "1" * 32, age_hours=48)
        output = frames / "9" / f"timelapse_{'1' * 32}.mp4"
        output.write_bytes(b"half")
        keep = frames / "9" / "holiday.mp4"  # not our naming: not ours
        keep.write_bytes(b"x")
        assert lt.sweep_orphaned_frames() == 1
        assert not output.exists() and keep.is_file()
        keep.unlink()
        _retained(frames, 8, "2" * 32, age_hours=48)
        lt.sweep_orphaned_frames()
        assert not (frames / "8").exists()

    def test_a_link_in_the_tree_is_not_followed(self, frames, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "layer_00001.jpg").write_bytes(b"x")
        lt._write_manifest(
            outside,
            {"kind": lt.MANIFEST_KIND, "session_id": outside.name, "state": lt.STATE_RETAINED, "retained_at": 0},
        )
        (frames / "1").mkdir(parents=True)
        link = frames / "1" / outside.name
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("this platform or account cannot create a directory link")
        assert lt.sweep_orphaned_frames() == 0
        assert (outside / "layer_00001.jpg").is_file()

    def test_a_missing_tree_is_not_an_error(self, frames):
        assert not frames.exists()
        assert lt.sweep_orphaned_frames() == 0
