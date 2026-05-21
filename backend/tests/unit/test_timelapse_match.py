"""Regression tests for the timelapse start-time matcher (upstream Bambuddy #1278).

``_match_timelapse_by_timestamp`` matches a Bambu timelapse filename (which
embeds the print START time in the printer's local clock) against an archive's
``started_at`` across a small set of common UTC offsets, and refuses to
auto-pick when two *different* videos match almost equally well — surfacing the
manual-selection fallback instead of attaching the wrong video.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from backend.app.api.routes.archives import _match_timelapse_by_timestamp


def _vid(name: str) -> dict:
    return {"name": name}


# Reporter scenario: P2S in LAN-Only mode, printer clock drifted +8h from UTC.
_STALE = _vid("video_2026-05-08_09-41-29.mp4")  # archive 1's video
_CORRECT_A2 = _vid("video_2026-05-09_00-42-42.mp4")  # archive 2's video
_ARCHIVE1_START = datetime(2026, 5, 8, 1, 41, 29)  # = filename 09:41:29 − 8h
_ARCHIVE2_START = datetime(2026, 5, 8, 16, 39, 9)


def test_issue_1278_archive2_refuses_to_auto_pick_ambiguous():
    """With both videos present, archive 2's start sits within minutes of the
    stale video (offset −7) AND its own video (offset +8) — too close to call,
    so the matcher refuses rather than attaching archive 1's stale video."""
    candidate, diff = _match_timelapse_by_timestamp([_STALE, _CORRECT_A2], _ARCHIVE2_START)
    assert candidate is None
    assert diff is None


def test_issue_1278_archive1_still_matches_unambiguously():
    """Archive 1's start is an exact hit for its own video at offset +8, and the
    other video is a full day away (outside tolerance) — clean auto-pick."""
    candidate, diff = _match_timelapse_by_timestamp([_STALE, _CORRECT_A2], _ARCHIVE1_START)
    assert candidate is _STALE
    assert diff == timedelta(0)


def test_archive2_resolves_when_stale_video_removed():
    """Once the stale video is gone, archive 2 matches its own video cleanly."""
    candidate, diff = _match_timelapse_by_timestamp([_CORRECT_A2], _ARCHIVE2_START)
    assert candidate is _CORRECT_A2
    assert diff == timedelta(minutes=3, seconds=33)


def test_none_start_returns_none():
    assert _match_timelapse_by_timestamp([_STALE], None) == (None, None)


def test_non_timestamp_filenames_are_skipped():
    candidate, diff = _match_timelapse_by_timestamp([_vid("thumbnail.png"), _vid("notes.txt")], _ARCHIVE1_START)
    assert candidate is None
    assert diff is None


def test_no_candidate_within_tolerance_returns_none():
    """A video whose only timestamp is days away (no offset brings it within
    the 4 h tolerance) is not matched."""
    far = _vid("video_2026-01-01_00-00-00.mp4")
    assert _match_timelapse_by_timestamp([far], _ARCHIVE1_START) == (None, None)


def test_same_video_multiple_offsets_not_ambiguous():
    """The ambiguity guard only fires across *different* videos. A single video
    matching at several offsets is still an unambiguous pick."""
    only = _vid("video_2026-05-08_09-41-29.mp4")
    candidate, diff = _match_timelapse_by_timestamp([only], _ARCHIVE1_START)
    assert candidate is only
    assert diff == timedelta(0)


def test_well_separated_different_videos_still_auto_pick():
    """Two different videos that match very differently (gap > 15 min margin)
    auto-pick the closer one."""
    start = datetime(2026, 6, 1, 12, 0, 0)
    close = _vid("video_2026-06-01_12-00-30.mp4")  # +30 s at offset 0
    far = _vid("video_2026-06-01_15-30-00.mp4")  # +3h30m at offset 0 (within 4h tol)
    candidate, _ = _match_timelapse_by_timestamp([close, far], start)
    assert candidate is close


@pytest.mark.parametrize("offset", [0, 8, -8, 7, -7, 1, -1])
def test_each_supported_offset_auto_matches(offset):
    """A video whose printer-local filename equals start + offset is matched at
    that offset (covers EU/JST/AEST as well as the CST/UTC cases)."""
    start = datetime(2026, 7, 4, 10, 0, 0)
    local = start + timedelta(hours=offset)
    name = f"video_{local.strftime('%Y-%m-%d_%H-%M-%S')}.mp4"
    candidate, diff = _match_timelapse_by_timestamp([_vid(name)], start)
    assert candidate is not None
    assert diff == timedelta(0)
