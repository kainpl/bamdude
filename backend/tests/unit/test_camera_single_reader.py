"""One camera, one reader — the two axes of it (#2705, #2707).

Bambu firmware permits exactly one camera connection, and a USB camera permits
exactly one V4L2 handle. A capture that races another reader does not degrade,
it **fails**.

`inv-single-camera-socket` covered one axis: a background capturer must not
compete with an attached **viewer**. Two holes remained, and both are here:

* **capturer vs capturer.** With no viewer attached, every consumer correctly
  concluded it was not competing with a viewer — and then collided with the
  others. Eight paths reach the built-in capture independently.
* **the external camera had neither.** Nothing ever populated the buffer for
  external cameras, so every one-shot consumer found it empty and opened its own
  handle. Upstream measured **0 of 87** and **0 of 105** layer-timelapse
  captures on prints watched throughout, and finish photos with no image.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from backend.app.services import camera as camera_service, external_camera as external_service


@pytest.fixture(autouse=True)
def _clear_registries():
    camera_service._inflight_captures.clear()
    external_service._inflight_captures.clear()
    yield
    camera_service._inflight_captures.clear()
    external_service._inflight_captures.clear()


@pytest.mark.asyncio
class TestTheBuiltInPathCoalesces:
    async def test_simultaneous_callers_open_one_connection(self) -> None:
        """The reported collision: an Obico poll and a snapshot 207 ms apart
        opened two RTSP sockets and knocked over the camera wall's stream."""
        opens = 0

        async def _slow_capture(ip, code, model, timeout):
            nonlocal opens
            opens += 1
            await asyncio.sleep(0.05)
            return b"\xff\xd8FRAME"

        with patch.object(camera_service, "_capture_camera_frame_bytes_uncoalesced", _slow_capture):
            results = await asyncio.gather(
                *(camera_service.capture_camera_frame_bytes("10.0.0.5", "code", "P1S") for _ in range(5))
            )

        assert opens == 1, "five simultaneous consumers must share one connection"
        assert results == [b"\xff\xd8FRAME"] * 5

    async def test_a_later_call_captures_fresh(self) -> None:
        """It coalesces; it does NOT cache. Plate detection and the finish photo
        decide things about a running print from these frames, and a stale one
        is worse than a slow one — #1397 was a finish photo showing the bed
        already lowered."""
        opens = 0

        async def _capture(ip, code, model, timeout):
            nonlocal opens
            opens += 1
            return b"frame"

        with patch.object(camera_service, "_capture_camera_frame_bytes_uncoalesced", _capture):
            await camera_service.capture_camera_frame_bytes("10.0.0.5", "code", "P1S")
            await camera_service.capture_camera_frame_bytes("10.0.0.5", "code", "P1S")

        assert opens == 2

    async def test_different_printers_do_not_share(self) -> None:
        """Keyed by IP, because IP is what the one-connection limit applies to."""
        seen: list[str] = []

        async def _capture(ip, code, model, timeout):
            seen.append(ip)
            await asyncio.sleep(0.02)
            return b"frame"

        with patch.object(camera_service, "_capture_camera_frame_bytes_uncoalesced", _capture):
            await asyncio.gather(
                camera_service.capture_camera_frame_bytes("10.0.0.5", "c", "P1S"),
                camera_service.capture_camera_frame_bytes("10.0.0.6", "c", "P1S"),
            )

        assert sorted(seen) == ["10.0.0.5", "10.0.0.6"]

    async def test_a_follower_takes_its_own_turn_when_the_leader_fails(self) -> None:
        """A follower must not inherit a failure it never had a chance to avoid —
        and by then the leader has finished, so there is no socket to compete
        with."""
        attempts = 0

        async def _capture(ip, code, model, timeout):
            nonlocal attempts
            attempts += 1
            await asyncio.sleep(0.02)
            return None if attempts == 1 else b"second"

        with patch.object(camera_service, "_capture_camera_frame_bytes_uncoalesced", _capture):
            leader, follower = await asyncio.gather(
                camera_service.capture_camera_frame_bytes("10.0.0.5", "c", "P1S"),
                camera_service.capture_camera_frame_bytes("10.0.0.5", "c", "P1S"),
            )

        assert leader is None
        assert follower == b"second"

    async def test_the_registry_empties_itself(self) -> None:
        """A leaked entry would make every later caller wait on a dead task."""

        async def _capture(ip, code, model, timeout):
            return b"frame"

        with patch.object(camera_service, "_capture_camera_frame_bytes_uncoalesced", _capture):
            await camera_service.capture_camera_frame_bytes("10.0.0.5", "c", "P1S")

        await asyncio.sleep(0)
        assert camera_service._inflight_captures == {}
        assert camera_service.capture_in_flight("10.0.0.5") is False


@pytest.mark.asyncio
class TestTheExternalPathCoalesces:
    async def test_simultaneous_callers_open_one_handle(self) -> None:
        opens = 0

        async def _slow(url, camera_type, timeout, snapshot_url):
            nonlocal opens
            opens += 1
            await asyncio.sleep(0.05)
            return b"\xff\xd8USB"

        with patch.object(external_service, "_capture_frame_uncoalesced", _slow):
            results = await asyncio.gather(*(external_service.capture_frame("/dev/video0", "usb") for _ in range(4)))

        assert opens == 1
        assert results == [b"\xff\xd8USB"] * 4

    async def test_a_snapshot_override_is_a_different_source(self) -> None:
        """A snapshot override fetches a completely different endpoint, so two
        callers that disagree about it are not asking for the same thing."""
        keys: list = []

        async def _capture(url, camera_type, timeout, snapshot_url):
            keys.append(snapshot_url)
            await asyncio.sleep(0.02)
            return b"frame"

        with patch.object(external_service, "_capture_frame_uncoalesced", _capture):
            await asyncio.gather(
                external_service.capture_frame("http://cam/stream", "mjpeg"),
                external_service.capture_frame("http://cam/stream", "mjpeg", snapshot_url="http://cam/frame.jpeg"),
            )

        assert sorted(k or "" for k in keys) == ["", "http://cam/frame.jpeg"]


class TestTheViewerRuleIsOneFunction:
    def test_it_answers_defer_and_frame_together(self) -> None:
        """Every consumer used to pair is_stream_active with get_buffered_frame
        itself, and that pairing IS the invariant — each reimplementation is a
        chance to get it subtly wrong."""
        from backend.app.api.routes import camera as camera_routes

        with (
            patch.object(camera_routes, "is_stream_active", return_value=True),
            patch.object(camera_routes, "get_buffered_frame", return_value=b"LIVE"),
        ):
            assert camera_routes.live_frame_for_capture(1) == (True, b"LIVE")

    def test_a_viewer_with_an_empty_buffer_still_means_stand_down(self) -> None:
        """The #1348 rule, and the easiest half to get wrong: an empty buffer is
        the window between frames, not permission to open a second handle."""
        from backend.app.api.routes import camera as camera_routes

        with (
            patch.object(camera_routes, "is_stream_active", return_value=True),
            patch.object(camera_routes, "get_buffered_frame", return_value=None),
        ):
            assert camera_routes.live_frame_for_capture(1) == (True, None)

    def test_no_viewer_means_capture_your_own(self) -> None:
        from backend.app.api.routes import camera as camera_routes

        with patch.object(camera_routes, "is_stream_active", return_value=False):
            assert camera_routes.live_frame_for_capture(1) == (False, None)
