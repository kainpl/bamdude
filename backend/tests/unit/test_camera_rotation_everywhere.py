"""``camera_rotation`` reaches every saved still, exactly once (#2708).

The setting was wired into the notification-snapshot path alone. So a camera
mounted upside-down produced a right-way-up snapshot and an upside-down
timelapse **of the same print** — which reads as a bug in the timelapse rather
than as a setting that only half applies. Finish photos had the same gap.

"Exactly once" is the whole difficulty. The in-print frame bank is filled from
the snapshot path, so those bytes are *already* rotated by the time anything
else sees them, while every other source is a raw grab. The producer decides,
records the answer in ``_stage22_finish_frames``, and no consumer rotates.
"""

from __future__ import annotations

import io
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image

from backend.app import main as main_module
from backend.app.main import _inprint_frame_bank, _stage22_finish_frames, on_finish_photo_moment
from backend.app.services.camera import apply_camera_rotation, apply_camera_rotation_to_file

_LOG = logging.getLogger(__name__)


def _jpeg(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (10, 20, 30)).save(buf, format="JPEG")
    return buf.getvalue()


def _size(data: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(data)).size


class TestTheRotationItself:
    def test_ninety_degrees_swaps_the_dimensions(self) -> None:
        assert _size(apply_camera_rotation(_jpeg(640, 480), 90, _LOG)) == (480, 640)

    def test_one_eighty_keeps_them(self) -> None:
        assert _size(apply_camera_rotation(_jpeg(640, 480), 180, _LOG)) == (640, 480)

    def test_zero_returns_the_very_same_object(self) -> None:
        """Not merely equal — identical, so "was this rotated?" can be answered
        by identity on the paths that care."""
        original = _jpeg(64, 48)
        assert apply_camera_rotation(original, 0, _LOG) is original

    def test_a_frame_it_cannot_decode_is_passed_through(self) -> None:
        """A still that could not be rotated beats no still at all — this runs
        on the finish-photo path, where the alternative is losing the photo."""
        assert apply_camera_rotation(b"not a jpeg", 90, _LOG) == b"not a jpeg"

    def test_rotating_twice_is_not_the_same_as_once(self) -> None:
        """States the reason the "exactly once" bookkeeping exists at all."""
        once = apply_camera_rotation(_jpeg(640, 480), 90, _LOG)
        twice = apply_camera_rotation(once, 90, _LOG)
        assert _size(once) == (480, 640)
        assert _size(twice) == (640, 480), "a double rotation lands back on its side"


@pytest.mark.asyncio
class TestRotatingAFileInPlace:
    async def test_the_file_is_rewritten_rotated(self, tmp_path) -> None:
        path = tmp_path / "finish.jpg"
        path.write_bytes(_jpeg(640, 480))

        await apply_camera_rotation_to_file(path, 90, _LOG)

        assert _size(path.read_bytes()) == (480, 640)

    async def test_zero_leaves_the_file_untouched(self, tmp_path) -> None:
        path = tmp_path / "finish.jpg"
        original = _jpeg(640, 480)
        path.write_bytes(original)

        await apply_camera_rotation_to_file(path, 0, _LOG)

        assert path.read_bytes() == original

    async def test_a_missing_file_does_not_raise(self, tmp_path) -> None:
        """Best-effort by design: the photo is already saved by this point."""
        await apply_camera_rotation_to_file(tmp_path / "gone.jpg", 90, _LOG)


@pytest.mark.asyncio
class TestTimelapseFrames:
    async def test_each_frame_is_rotated_before_it_is_written(self, tmp_path, monkeypatch) -> None:
        """ffmpeg reads the frames straight off disk at stitch time, so a frame
        saved the wrong way up stays that way in the finished video."""
        from backend.app.core.config import settings as app_settings
        from backend.app.services import layer_timelapse

        monkeypatch.setattr(app_settings, "base_dir", tmp_path)
        monkeypatch.setattr(layer_timelapse, "capture_frame", AsyncMock(return_value=_jpeg(640, 480)))

        session = layer_timelapse.TimelapseSession(
            printer_id=1, archive_id=None, camera_url="http://cam", camera_type="mjpeg", rotation=90
        )
        assert await session.capture_layer(1) is True

        written = next(session.frames_dir.glob("layer_*.jpg"))
        assert _size(written.read_bytes()) == (480, 640)

    async def test_an_unrotated_printer_writes_the_frame_as_captured(self, tmp_path, monkeypatch) -> None:
        from backend.app.core.config import settings as app_settings
        from backend.app.services import layer_timelapse

        monkeypatch.setattr(app_settings, "base_dir", tmp_path)
        original = _jpeg(640, 480)
        monkeypatch.setattr(layer_timelapse, "capture_frame", AsyncMock(return_value=original))

        session = layer_timelapse.TimelapseSession(
            printer_id=2, archive_id=None, camera_url="http://cam", camera_type="mjpeg"
        )
        await session.capture_layer(1)

        assert next(session.frames_dir.glob("layer_*.jpg")).read_bytes() == original


def _session_factory(printer):
    @asynccontextmanager
    async def fake_session():
        async def execute(_stmt):
            return SimpleNamespace(scalar_one_or_none=lambda: printer)

        yield SimpleNamespace(execute=execute)

    return fake_session


def _printer(rotation: int):
    return SimpleNamespace(
        id=7,
        ip_address="10.0.0.5",
        access_code="12345678",
        model="P1S",
        camera_rotation=rotation,
        external_camera_enabled=False,
        external_camera_url=None,
        external_camera_type=None,
        external_camera_snapshot_url=None,
    )


@pytest.fixture(autouse=True)
def _clear_state():
    _stage22_finish_frames.clear()
    _inprint_frame_bank.clear()
    yield
    _stage22_finish_frames.clear()
    _inprint_frame_bank.clear()


@pytest.mark.asyncio
class TestTheFinishPhotoIsRotatedOnce:
    async def test_a_raw_grab_is_rotated_by_the_producer(self, monkeypatch) -> None:
        monkeypatch.setattr(main_module, "async_session", _session_factory(_printer(90)))

        with (
            patch("backend.app.api.routes.settings.get_setting", new=AsyncMock(return_value="true")),
            patch("backend.app.api.routes.camera.get_buffered_frame", return_value=_jpeg(640, 480)),
        ):
            await on_finish_photo_moment(7, {"trigger": "stage_22", "timelapse_was_active": False})

        assert _size(_stage22_finish_frames[7]) == (480, 640)

    async def test_the_banked_frame_is_not_rotated_again(self, monkeypatch) -> None:
        """It was rotated when it was banked — it comes from the snapshot path.
        Rotating here would put it back on its side, which is the failure the
        bookkeeping exists to prevent."""
        monkeypatch.setattr(main_module, "async_session", _session_factory(_printer(90)))
        already_rotated = apply_camera_rotation(_jpeg(640, 480), 90, _LOG)
        _inprint_frame_bank[7] = already_rotated

        with patch("backend.app.api.routes.settings.get_setting", new=AsyncMock(return_value="true")):
            await on_finish_photo_moment(7, {"trigger": "finish_state", "timelapse_was_active": False})

        assert _stage22_finish_frames[7] == already_rotated
        assert _size(_stage22_finish_frames[7]) == (480, 640)

    async def test_an_unrotated_printer_caches_the_frame_as_captured(self, monkeypatch) -> None:
        monkeypatch.setattr(main_module, "async_session", _session_factory(_printer(0)))
        original = _jpeg(640, 480)

        with (
            patch("backend.app.api.routes.settings.get_setting", new=AsyncMock(return_value="true")),
            patch("backend.app.api.routes.camera.get_buffered_frame", return_value=original),
        ):
            await on_finish_photo_moment(7, {"trigger": "stage_22", "timelapse_was_active": False})

        assert _stage22_finish_frames[7] == original


class TestEverySiteIsWired:
    def test_all_three_timelapse_starts_pass_the_rotation(self) -> None:
        """There are three of them — library print, fallback archive and reprint.
        One left behind is a whole path that silently keeps the old behaviour."""
        import inspect

        source = inspect.getsource(main_module)
        assert source.count('rotation=getattr(printer, "camera_rotation", 0) or 0,') >= 3
