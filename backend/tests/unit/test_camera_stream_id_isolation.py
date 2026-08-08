"""A departing camera stream must not clean up its successor (upstream #2707).

Close a camera view and reopen it inside the teardown window and two things used
to happen, both of them silent:

* the fan-out stream id was the constant ``f"{printer_id}-fanout"``, so the old
  generator's ``finally`` popped the **new** stream's ``_active_streams`` entry;
* the per-printer frame state (`_last_frames`, `_last_frame_times`,
  `_stream_start_times`) was popped unconditionally, taking the new stream's
  buffer with it.

Neither looks like a camera bug from the outside. ``is_stream_active`` reports no
viewer for a live stream, so snapshots and Obico polling open a **second**
upstream socket — exactly what ``inv-single-camera-socket`` exists to prevent on
firmware that permits only one — and the orphan janitor kills the running ffmpeg
as unregistered.

The external path already had unique ids (#2675); this is the same fix on the
other path, so the two now read alike.
"""

from __future__ import annotations

import re

import pytest

from backend.app.api.routes import camera as camera_routes


@pytest.fixture(autouse=True)
def _clean_registries():
    for reg in (
        camera_routes._active_streams,
        camera_routes._active_chamber_streams,
        camera_routes._last_frames,
        camera_routes._last_frame_times,
        camera_routes._stream_start_times,
    ):
        reg.clear()
    yield
    for reg in (
        camera_routes._active_streams,
        camera_routes._active_chamber_streams,
        camera_routes._last_frames,
        camera_routes._last_frame_times,
        camera_routes._stream_start_times,
    ):
        reg.clear()


class TestFanoutStreamId:
    def test_two_fanout_ids_for_one_printer_differ(self):
        # The shape is asserted, not just the inequality: both scanners key on
        # the `{printer_id}-` prefix, so a "unique" id that dropped it would
        # make the stream invisible to /camera/stop and the janitor instead.
        ids = {camera_routes._new_fanout_stream_id(7) for _ in range(20)}
        assert len(ids) == 20
        assert all(re.fullmatch(r"7-fanout-[0-9a-f]{8}", sid) for sid in ids)

    def test_the_prefix_still_identifies_the_printer(self):
        camera_routes._active_streams[camera_routes._new_fanout_stream_id(3)] = object()
        assert camera_routes.is_stream_active(3) is True
        assert camera_routes.is_stream_active(30) is False  # not a prefix match on "3"


class TestFrameStateRelease:
    def _seed(self, printer_id: int) -> None:
        camera_routes._last_frames[printer_id] = b"jpeg"
        camera_routes._last_frame_times[printer_id] = 123.0
        camera_routes._stream_start_times[printer_id] = 100.0

    def test_the_last_stream_out_clears_the_state(self):
        self._seed(5)
        camera_routes._release_printer_frame_state(5)
        assert 5 not in camera_routes._last_frames
        assert 5 not in camera_routes._last_frame_times
        assert 5 not in camera_routes._stream_start_times

    def test_a_departing_stream_leaves_a_survivors_state_alone(self):
        """The bug, stated the way it happens: viewer A leaves, viewer B stays."""
        self._seed(5)
        camera_routes._active_streams["5-fanout-deadbeef"] = object()  # B is live
        camera_routes._release_printer_frame_state(5)  # A's finally
        assert camera_routes._last_frames[5] == b"jpeg"
        assert camera_routes._stream_start_times[5] == 100.0

    def test_a_chamber_stream_counts_as_a_survivor_too(self):
        # The chamber and RTSP paths share the per-printer dicts, so the check
        # has to span both registries or one path clears the other's state.
        self._seed(5)
        camera_routes._active_chamber_streams["5-chamber"] = object()
        camera_routes._release_printer_frame_state(5)
        assert 5 in camera_routes._last_frames

    def test_another_printers_stream_is_not_a_survivor(self):
        self._seed(5)
        camera_routes._active_streams["9-fanout-deadbeef"] = object()
        camera_routes._release_printer_frame_state(5)
        assert 5 not in camera_routes._last_frames

    def test_no_printer_id_is_a_no_op(self):
        # Both generators accept printer_id=None; the guard belongs in one place.
        camera_routes._release_printer_frame_state(None)
