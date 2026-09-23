"""A departing worker relay must not clean up its successor's frame state."""

from __future__ import annotations

import re

import pytest

from backend.app.api.routes import camera as camera_routes


@pytest.fixture(autouse=True)
def _clean_registries():
    for reg in (
        camera_routes._active_worker_streams,
        camera_routes._last_frames,
        camera_routes._last_frame_times,
        camera_routes._stream_start_times,
    ):
        reg.clear()
    yield
    for reg in (
        camera_routes._active_worker_streams,
        camera_routes._last_frames,
        camera_routes._last_frame_times,
        camera_routes._stream_start_times,
    ):
        reg.clear()


class TestFanoutStreamId:
    def test_two_fanout_ids_for_one_printer_differ(self):
        # Relay identifiers remain unique across close/reopen races.
        ids = {camera_routes._new_fanout_stream_id(7) for _ in range(20)}
        assert len(ids) == 20
        assert all(re.fullmatch(r"7-fanout-[0-9a-f]{8}", sid) for sid in ids)

    def test_the_registry_identifies_the_printer(self):
        camera_routes._active_worker_streams[3] = ("lease", "rtsp")
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
        camera_routes._active_worker_streams[5] = ("successor", "rtsp")  # B is live
        camera_routes._release_printer_frame_state(5)  # A's finally
        assert camera_routes._last_frames[5] == b"jpeg"
        assert camera_routes._stream_start_times[5] == 100.0

    def test_a_chamber_worker_stream_counts_as_a_survivor_too(self):
        self._seed(5)
        camera_routes._active_worker_streams[5] = ("successor", "chamber_image")
        camera_routes._release_printer_frame_state(5)
        assert 5 in camera_routes._last_frames

    def test_another_printers_stream_is_not_a_survivor(self):
        self._seed(5)
        camera_routes._active_worker_streams[9] = ("other", "rtsp")
        camera_routes._release_printer_frame_state(5)
        assert 5 not in camera_routes._last_frames

    def test_no_printer_id_is_a_no_op(self):
        # A missing identity is a no-op.
        camera_routes._release_printer_frame_state(None)
