"""Unit tests for Obico detection service (#172)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.schemas.settings import AppSettingsUpdate
from backend.app.services.obico_detection import (
    FRAME_CACHE_TTL,
    ObicoDetectionService,
    _frame_cache,
    pop_frame,
    stash_frame,
)
from backend.app.services.obico_smoothing import WARMUP_FRAMES

FAKE_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"


class TestSettingsSchemaValidators:
    """Guard rails on the new obico_* AppSettings fields."""

    def test_sensitivity_accepts_valid_values(self):
        for value in ("low", "medium", "high"):
            u = AppSettingsUpdate(obico_sensitivity=value)
            assert u.obico_sensitivity == value

    def test_sensitivity_rejects_garbage(self):
        with pytest.raises(ValueError, match="obico_sensitivity"):
            AppSettingsUpdate(obico_sensitivity="extreme")

    def test_action_accepts_valid_values(self):
        for value in ("notify", "pause", "pause_and_off"):
            assert AppSettingsUpdate(obico_action=value).obico_action == value

    def test_action_rejects_garbage(self):
        with pytest.raises(ValueError, match="obico_action"):
            AppSettingsUpdate(obico_action="explode")

    def test_enabled_printers_accepts_empty(self):
        assert AppSettingsUpdate(obico_enabled_printers="").obico_enabled_printers == ""
        assert AppSettingsUpdate(obico_enabled_printers=None).obico_enabled_printers is None

    def test_enabled_printers_accepts_int_array(self):
        u = AppSettingsUpdate(obico_enabled_printers="[1, 2, 3]")
        assert u.obico_enabled_printers == "[1, 2, 3]"

    def test_enabled_printers_rejects_non_json(self):
        with pytest.raises(ValueError, match="valid JSON"):
            AppSettingsUpdate(obico_enabled_printers="1,2,3")

    def test_enabled_printers_rejects_non_list(self):
        with pytest.raises(ValueError, match="JSON array"):
            AppSettingsUpdate(obico_enabled_printers='{"1": true}')

    def test_enabled_printers_rejects_non_int_elements(self):
        with pytest.raises(ValueError, match="JSON array"):
            AppSettingsUpdate(obico_enabled_printers='[1, "two"]')

    def test_poll_interval_bounds(self):
        with pytest.raises(ValueError):
            AppSettingsUpdate(obico_poll_interval=4)
        with pytest.raises(ValueError):
            AppSettingsUpdate(obico_poll_interval=121)
        assert AppSettingsUpdate(obico_poll_interval=10).obico_poll_interval == 10


class TestGetStatus:
    def test_empty_initial_status(self):
        svc = ObicoDetectionService()
        s = svc.get_status()
        assert s["is_running"] is False
        assert s["per_printer"] == {}
        assert s["history"] == []
        assert "low" in s["thresholds"] and "high" in s["thresholds"]


class TestTestConnection:
    @pytest.mark.asyncio
    async def test_empty_url_via_route(self):
        """Service does not special-case empty URL — the route does."""
        svc = ObicoDetectionService()
        # This will fail DNS/connect, but should return ok=False
        result = await svc.test_connection("http://nonexistent-obico-host-xyz.invalid:3333")
        assert result["ok"] is False
        assert result["error"] is not None

    @pytest.mark.asyncio
    async def test_healthy_response_is_ok(self):
        svc = ObicoDetectionService()
        mock_response = MagicMock(status_code=200, text="ok")
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("backend.app.services.obico_detection.httpx.AsyncClient", return_value=mock_client):
            result = await svc.test_connection("http://obico:3333")
        assert result["ok"] is True
        assert result["status_code"] == 200
        assert result["body"] == "ok"
        assert result["error"] is None

    @pytest.mark.asyncio
    async def test_non_ok_body_is_not_ok(self):
        svc = ObicoDetectionService()
        mock_response = MagicMock(status_code=200, text="something else")
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("backend.app.services.obico_detection.httpx.AsyncClient", return_value=mock_client):
            result = await svc.test_connection("http://obico:3333/")
        assert result["ok"] is False
        assert result["body"] == "something else"


class TestPollOneStateLifecycle:
    """Confirms per-printer state is reset when a new print starts."""

    @pytest.mark.asyncio
    async def test_new_task_name_resets_state(self):
        svc = ObicoDetectionService()
        # Seed a state that has been running for a while
        from backend.app.services.obico_smoothing import PrintState

        seeded = PrintState()
        for _ in range(WARMUP_FRAMES + 5):
            seeded.update(0.5)
        svc._states[1] = seeded
        svc._state_keys[1] = "old_task"
        svc._action_fired[1] = True

        settings = {
            "enabled": True,
            "ml_url": "http://obico:3333",
            "sensitivity": "medium",
            "action": "notify",
            "poll_interval": 10,
            "enabled_printers": None,
            "external_url": "http://bamdude:8000",
        }
        status = MagicMock(state="RUNNING", task_name="new_task", subtask_name="")

        mock_response = MagicMock()
        mock_response.json.return_value = {"detections": []}
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("backend.app.services.obico_detection.httpx.AsyncClient", return_value=mock_client),
            patch.object(svc, "_capture_frame", new=AsyncMock(return_value=FAKE_JPEG)),
        ):
            await svc._check_printer(1, status, settings)

        # State was reset (frame_count is 1 after the single update, not 36)
        assert svc._states[1].frame_count == 1
        assert svc._state_keys[1] == "new_task"
        assert svc._action_fired[1] is False

    @pytest.mark.asyncio
    async def test_ml_api_error_does_not_crash(self):
        svc = ObicoDetectionService()
        settings = {
            "enabled": True,
            "ml_url": "http://obico:3333",
            "sensitivity": "medium",
            "action": "notify",
            "poll_interval": 10,
            "enabled_printers": None,
            "external_url": "http://bamdude:8000",
        }
        status = MagicMock(state="RUNNING", task_name="job", subtask_name="")

        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=RuntimeError("connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("backend.app.services.obico_detection.httpx.AsyncClient", return_value=mock_client),
            patch.object(svc, "_capture_frame", new=AsyncMock(return_value=FAKE_JPEG)),
        ):
            await svc._check_printer(1, status, settings)

        assert svc._last_error is not None
        assert "connection refused" in svc._last_error

    @pytest.mark.asyncio
    async def test_ml_api_empty_exception_message_falls_back_to_type(self):
        """If str(exc) is empty, log the exception class name instead of a blank suffix."""
        svc = ObicoDetectionService()
        settings = {
            "enabled": True,
            "ml_url": "http://obico:3333",
            "sensitivity": "medium",
            "action": "notify",
            "poll_interval": 10,
            "enabled_printers": None,
            "external_url": "http://bamdude:8000",
        }
        status = MagicMock(state="RUNNING", task_name="job", subtask_name="")

        class _SilentError(Exception):
            def __str__(self) -> str:
                return ""

        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=_SilentError())
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("backend.app.services.obico_detection.httpx.AsyncClient", return_value=mock_client),
            patch.object(svc, "_capture_frame", new=AsyncMock(return_value=FAKE_JPEG)),
        ):
            await svc._check_printer(1, status, settings)

        assert svc._last_error is not None
        assert "_SilentError" in svc._last_error
        # The suffix is never blank
        assert not svc._last_error.rstrip().endswith(":")

    @pytest.mark.asyncio
    async def test_failure_fires_action_only_once(self):
        """Once a failure has fired for a print, subsequent failures should not re-fire."""
        svc = ObicoDetectionService()
        settings = {
            "enabled": True,
            "ml_url": "http://obico:3333",
            "sensitivity": "medium",
            "action": "notify",
            "poll_interval": 10,
            "enabled_printers": None,
            "external_url": "http://bamdude:8000",
        }
        status = MagicMock(state="RUNNING", task_name="job", subtask_name="")

        # Seed state so the next frame crosses HIGH immediately
        from backend.app.services.obico_smoothing import PrintState

        seeded = PrintState()
        for _ in range(WARMUP_FRAMES + 500):
            seeded.update(1.0)
        svc._states[1] = seeded
        svc._state_keys[1] = "job"
        svc._action_fired[1] = False

        mock_response = MagicMock()
        mock_response.json.return_value = {"detections": [["failure", 0.9, [0, 0, 1, 1]]]}
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("backend.app.services.obico_detection.httpx.AsyncClient", return_value=mock_client),
            patch("backend.app.services.obico_actions.execute_action", new=AsyncMock()) as mock_action,
            patch.object(svc, "_capture_frame", new=AsyncMock(return_value=FAKE_JPEG)),
        ):
            await svc._check_printer(1, status, settings)
            assert mock_action.call_count == 1
            await svc._check_printer(1, status, settings)
            # Second call must not dispatch again
            assert mock_action.call_count == 1


class TestFrameCache:
    """One-shot JPEG cache that lets us sidestep Obico's 5s read timeout.

    Obico's ML API fetches snapshots via `GET /p/?img=URL` with `timeout=(0.1, 5)`.
    Our /camera/snapshot can exceed that on cold calls (RTSP keyframe wait). So the
    detection loop captures locally, stashes the JPEG bytes under a nonce, then hands
    Obico a URL that returns those bytes instantly. The cache is single-use + TTLed
    so a leaked nonce can't be replayed.
    """

    def setup_method(self):
        _frame_cache.clear()

    @pytest.mark.asyncio
    async def test_stash_and_pop_roundtrip(self):
        nonce = await stash_frame(FAKE_JPEG)
        assert nonce  # non-empty URL-safe token
        data = await pop_frame(nonce)
        assert data == FAKE_JPEG

    @pytest.mark.asyncio
    async def test_nonce_is_single_use(self):
        nonce = await stash_frame(FAKE_JPEG)
        assert await pop_frame(nonce) == FAKE_JPEG
        # Second pop returns None — caches replay protection
        assert await pop_frame(nonce) is None

    @pytest.mark.asyncio
    async def test_unknown_nonce_returns_none(self):
        assert await pop_frame("not-a-real-nonce") is None

    @pytest.mark.asyncio
    async def test_stash_produces_unique_nonces(self):
        nonces = {await stash_frame(FAKE_JPEG) for _ in range(10)}
        assert len(nonces) == 10

    @pytest.mark.asyncio
    async def test_expired_entries_are_pruned_on_stash(self):
        """New entries trigger pruning of TTL-expired ones — prevents unbounded growth."""
        # Manually seed an entry with a stale timestamp
        import time as time_module

        _frame_cache["stale-nonce"] = (FAKE_JPEG, time_module.monotonic() - FRAME_CACHE_TTL - 1)
        await stash_frame(FAKE_JPEG)
        # Stale entry was pruned
        assert "stale-nonce" not in _frame_cache

    @pytest.mark.asyncio
    async def test_pop_rejects_expired_nonce(self):
        """Even if the entry is still in the dict, an expired TTL returns None."""
        import time as time_module

        _frame_cache["aging-nonce"] = (FAKE_JPEG, time_module.monotonic() - FRAME_CACHE_TTL - 1)
        assert await pop_frame("aging-nonce") is None


class TestCheckPrinterUsesCachedFrameUrl:
    """The URL sent to Obico must point at our nonce endpoint, not /camera/snapshot."""

    def setup_method(self):
        _frame_cache.clear()

    @pytest.mark.asyncio
    async def test_ml_api_called_with_cached_frame_url(self):
        svc = ObicoDetectionService()
        settings = {
            "enabled": True,
            "ml_url": "http://obico:3333",
            "sensitivity": "medium",
            "action": "notify",
            "poll_interval": 10,
            "enabled_printers": None,
            "external_url": "http://bamdude:8000",
        }
        status = MagicMock(state="RUNNING", task_name="job", subtask_name="")

        mock_response = MagicMock()
        mock_response.json.return_value = {"detections": []}
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("backend.app.services.obico_detection.httpx.AsyncClient", return_value=mock_client),
            patch.object(svc, "_capture_frame", new=AsyncMock(return_value=FAKE_JPEG)),
        ):
            await svc._check_printer(1, status, settings)

        # ML API was called via GET (Obico's /p/ is GET-only)
        mock_client.get.assert_called_once()
        _args, kwargs = mock_client.get.call_args
        assert _args[0] == "http://obico:3333/p/"
        img_url = kwargs["params"]["img"]
        assert img_url.startswith("http://bamdude:8000/api/v1/obico/cached-frame/")
        # The path segment after /cached-frame/ is the nonce itself — that nonce must
        # resolve back to our stashed frame (single-use guarantees freshness).
        nonce = img_url.rsplit("/", 1)[-1]
        assert await pop_frame(nonce) == FAKE_JPEG

    @pytest.mark.asyncio
    async def test_capture_failure_skips_ml_call(self):
        """If we can't capture a frame, don't bother the ML API."""
        svc = ObicoDetectionService()
        settings = {
            "enabled": True,
            "ml_url": "http://obico:3333",
            "sensitivity": "medium",
            "action": "notify",
            "poll_interval": 10,
            "enabled_printers": None,
            "external_url": "http://bamdude:8000",
        }
        status = MagicMock(state="RUNNING", task_name="job", subtask_name="")

        mock_client = MagicMock()
        mock_client.get = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("backend.app.services.obico_detection.httpx.AsyncClient", return_value=mock_client),
            patch.object(svc, "_capture_frame", new=AsyncMock(return_value=None)),
        ):
            await svc._check_printer(1, status, settings)

        mock_client.get.assert_not_called()
        assert svc._last_error is not None
        assert "Failed to capture snapshot" in svc._last_error

    @pytest.mark.asyncio
    async def test_missing_external_url_skips_ml_call(self):
        """Without external_url, Obico can't reach our cached-frame endpoint."""
        svc = ObicoDetectionService()
        settings = {
            "enabled": True,
            "ml_url": "http://obico:3333",
            "sensitivity": "medium",
            "action": "notify",
            "poll_interval": 10,
            "enabled_printers": None,
            "external_url": "",
        }
        status = MagicMock(state="RUNNING", task_name="job", subtask_name="")

        mock_client = MagicMock()
        mock_client.get = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("backend.app.services.obico_detection.httpx.AsyncClient", return_value=mock_client),
            patch.object(svc, "_capture_frame", new=AsyncMock(return_value=FAKE_JPEG)),
        ):
            await svc._check_printer(1, status, settings)

        mock_client.get.assert_not_called()
        assert svc._last_error is not None
        assert "external_url" in svc._last_error

    @pytest.mark.asyncio
    async def test_successful_cycle_clears_previous_error(self):
        """A cold-start RTSP timeout sets _last_error; the next successful poll must clear it.

        Regression for #172: the Status card banner ("Failed to capture snapshot for
        printer 1") stuck around after a one-off cold-start failure even though every
        subsequent poll captured + detected successfully.
        """
        svc = ObicoDetectionService()
        settings = {
            "enabled": True,
            "ml_url": "http://obico:3333",
            "sensitivity": "medium",
            "action": "notify",
            "poll_interval": 10,
            "enabled_printers": None,
            "external_url": "http://bamdude:8000",
        }
        status = MagicMock(state="RUNNING", task_name="job", subtask_name="")

        # Seed a prior transient error, as would be left by a cold-start capture timeout.
        svc._last_error = "Failed to capture snapshot for printer 1"

        mock_response = MagicMock()
        mock_response.json.return_value = {"detections": []}
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("backend.app.services.obico_detection.httpx.AsyncClient", return_value=mock_client),
            patch.object(svc, "_capture_frame", new=AsyncMock(return_value=FAKE_JPEG)),
        ):
            await svc._check_printer(1, status, settings)

        assert svc._last_error is None


class TestCaptureFrameStreamActiveGate:
    """B.7 / upstream Bambuddy #1271 + #1348 — when a viewer is attached to
    the live camera, Obico's ``_capture_frame`` must NOT open a competing
    upstream socket. Some firmwares (notably X2D) enforce strict single-
    camera-connection and a second socket drops the viewer's stream.
    """

    @pytest.mark.asyncio
    async def test_built_in_camera_reuses_buffered_frame_when_viewer_attached(self):
        from backend.app.services.obico_detection import ObicoDetectionService

        svc = ObicoDetectionService()
        mock_printer = MagicMock()
        mock_printer.external_camera_enabled = False
        mock_printer.ip_address = "192.168.1.100"
        mock_printer.access_code = "12345678"
        mock_printer.model = "X2D"

        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)
        mock_db.get = AsyncMock(return_value=mock_printer)

        fresh_capture = AsyncMock(return_value=b"FRESH-SHOULD-NOT-BE-CALLED")

        with (
            patch("backend.app.services.obico_detection.async_session", return_value=mock_db),
            patch("backend.app.api.routes.camera.is_stream_active", return_value=True),
            patch("backend.app.api.routes.camera.get_buffered_frame", return_value=b"BUFFERED"),
            patch("backend.app.services.camera.capture_camera_frame_bytes", fresh_capture),
        ):
            result = await svc._capture_frame(1)

        assert result == b"BUFFERED"
        fresh_capture.assert_not_called()

    @pytest.mark.asyncio
    async def test_built_in_camera_skips_poll_when_viewer_attached_but_buffer_empty(self):
        """The #1348 race: viewer attached but buffer momentarily empty
        (startup or mid-reconnect). Must return None, NOT open competing
        socket. The next poll cycle will find a populated buffer.
        """
        from backend.app.services.obico_detection import ObicoDetectionService

        svc = ObicoDetectionService()
        mock_printer = MagicMock()
        mock_printer.external_camera_enabled = False
        mock_printer.ip_address = "192.168.1.100"
        mock_printer.access_code = "12345678"
        mock_printer.model = "X2D"

        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)
        mock_db.get = AsyncMock(return_value=mock_printer)

        fresh_capture = AsyncMock(return_value=b"FRESH-SHOULD-NOT-BE-CALLED")

        with (
            patch("backend.app.services.obico_detection.async_session", return_value=mock_db),
            patch("backend.app.api.routes.camera.is_stream_active", return_value=True),
            patch("backend.app.api.routes.camera.get_buffered_frame", return_value=None),
            patch("backend.app.services.camera.capture_camera_frame_bytes", fresh_capture),
        ):
            result = await svc._capture_frame(1)

        assert result is None
        fresh_capture.assert_not_called()

    @pytest.mark.asyncio
    async def test_built_in_camera_falls_back_to_fresh_capture_when_no_viewer(self):
        """No viewer attached → standard fresh-capture path."""
        from backend.app.services.obico_detection import ObicoDetectionService

        svc = ObicoDetectionService()
        mock_printer = MagicMock()
        mock_printer.external_camera_enabled = False
        mock_printer.ip_address = "192.168.1.100"
        mock_printer.access_code = "12345678"
        mock_printer.model = "X1C"

        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)
        mock_db.get = AsyncMock(return_value=mock_printer)

        fresh_capture = AsyncMock(return_value=b"FRESH")

        with (
            patch("backend.app.services.obico_detection.async_session", return_value=mock_db),
            patch("backend.app.api.routes.camera.is_stream_active", return_value=False),
            patch("backend.app.services.camera.capture_camera_frame_bytes", fresh_capture),
        ):
            result = await svc._capture_frame(1)

        assert result == b"FRESH"
        fresh_capture.assert_called_once()

    @pytest.mark.asyncio
    async def test_external_camera_path_unchanged_by_buffer_gate(self):
        """External-camera printers don't use the broadcaster; the gate
        must not apply to them."""
        from backend.app.services.obico_detection import ObicoDetectionService

        svc = ObicoDetectionService()
        mock_printer = MagicMock()
        mock_printer.external_camera_enabled = True
        mock_printer.external_camera_url = "http://camera.local/stream"
        mock_printer.external_camera_type = "mjpeg"
        mock_printer.external_camera_snapshot_url = "http://camera.local/snapshot.jpg"

        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)
        mock_db.get = AsyncMock(return_value=mock_printer)

        external_capture = AsyncMock(return_value=b"EXTERNAL")

        with (
            patch("backend.app.services.obico_detection.async_session", return_value=mock_db),
            patch("backend.app.services.external_camera.capture_frame", external_capture),
        ):
            result = await svc._capture_frame(1)

        assert result == b"EXTERNAL"
        external_capture.assert_called_once()


class TestIsStreamActiveHelper:
    """Pin the camera-route helpers' classification contract."""

    def test_no_streams_registered_returns_false(self):
        from backend.app.api.routes import camera

        camera._active_streams.clear()
        camera._active_chamber_streams.clear()
        assert camera.is_stream_active(42) is False
        assert camera.try_get_active_buffered_frame(42) is None

    def test_main_fanout_stream_registered_returns_true(self):
        from backend.app.api.routes import camera

        camera._active_streams.clear()
        camera._active_chamber_streams.clear()
        camera._active_streams["42-fanout"] = MagicMock()
        try:
            assert camera.is_stream_active(42) is True
        finally:
            camera._active_streams.pop("42-fanout", None)

    def test_chamber_stream_registered_returns_true(self):
        from backend.app.api.routes import camera

        camera._active_streams.clear()
        camera._active_chamber_streams.clear()
        camera._active_chamber_streams["42-chamber"] = (MagicMock(), MagicMock())
        try:
            assert camera.is_stream_active(42) is True
        finally:
            camera._active_chamber_streams.pop("42-chamber", None)

    def test_try_get_active_buffered_returns_frame_when_stream_active_and_buffered(self):
        from backend.app.api.routes import camera

        camera._active_streams.clear()
        camera._last_frames.clear()
        camera._active_streams["42-fanout"] = MagicMock()
        camera._last_frames[42] = b"BUFFERED"
        try:
            assert camera.try_get_active_buffered_frame(42) == b"BUFFERED"
        finally:
            camera._active_streams.pop("42-fanout", None)
            camera._last_frames.pop(42, None)

    def test_try_get_active_buffered_returns_none_when_no_stream_even_with_buffer(self):
        """Independent of stale-buffer state: no viewer → None. Prevents
        returning stale frames from a previously-attached viewer."""
        from backend.app.api.routes import camera

        camera._active_streams.clear()
        camera._active_chamber_streams.clear()
        camera._last_frames.clear()
        camera._last_frames[42] = b"STALE"
        try:
            assert camera.try_get_active_buffered_frame(42) is None
        finally:
            camera._last_frames.pop(42, None)
