"""Unit tests for :mod:`backend.app.services.camera_profiles`.

Ported from upstream Bambuddy commit 67cb5275 / #1395.

The default profile mirrors the historical hard-coded ffmpeg constants that
lived inline in ``backend/app/api/routes/camera.py``; the only override
today is P2S, which needs a much larger probesize than the X1 / H2 fast-
startup default to lock onto its slower keyframe pacing. The tests pin
both behaviours so a future model lookup regressing doesn't slip in.
"""

from __future__ import annotations

import pytest

from backend.app.services.camera_profiles import (
    DEFAULT_PROFILE,
    CameraProfile,
    get_camera_profile,
)


class TestDefaultProfileMatchesHistoricalValues:
    """Pin the X1 / H2 fast-startup defaults so the refactor doesn't shift
    the baseline behaviour."""

    def test_probesize_default(self):
        assert DEFAULT_PROFILE.probesize == 32

    def test_analyzeduration_default(self):
        assert DEFAULT_PROFILE.analyzeduration == 0

    def test_rtsp_reconnect_max_default(self):
        assert DEFAULT_PROFILE.rtsp_reconnect_max == 30

    def test_rtsp_reconnect_delay_default(self):
        assert DEFAULT_PROFILE.rtsp_reconnect_delay == pytest.approx(0.2)

    def test_no_extra_input_args_by_default(self):
        assert DEFAULT_PROFILE.extra_ffmpeg_input_args == ()


class TestGetCameraProfileFallback:
    def test_unknown_model_returns_default(self):
        assert get_camera_profile("definitely_not_a_printer") is DEFAULT_PROFILE

    def test_none_returns_default(self):
        assert get_camera_profile(None) is DEFAULT_PROFILE

    def test_empty_string_returns_default(self):
        assert get_camera_profile("") is DEFAULT_PROFILE

    def test_whitespace_only_returns_default(self):
        assert get_camera_profile("   ") is DEFAULT_PROFILE


class TestP2SOverride:
    """The single concrete override today — verify the relaxed-probe values."""

    def test_p2s_relaxed_probesize(self):
        profile = get_camera_profile("P2S")
        assert profile.probesize == 1_000_000
        assert profile is not DEFAULT_PROFILE

    def test_p2s_analyzeduration_increased(self):
        profile = get_camera_profile("P2S")
        assert profile.analyzeduration == 500_000

    def test_lookup_is_case_insensitive(self):
        assert get_camera_profile("p2s") is get_camera_profile("P2S")
        assert get_camera_profile("  p2s  ") is get_camera_profile("P2S")

    def test_p2s_keeps_default_reconnect_values(self):
        """The P2S override only adjusts probe knobs — reconnect cadence
        stays at the global default."""
        profile = get_camera_profile("P2S")
        assert profile.rtsp_reconnect_max == 30
        assert profile.rtsp_reconnect_delay == pytest.approx(0.2)

    def test_ssdp_internal_code_resolves_to_p2s(self):
        """N7 is the SSDP-only internal code for P2S; the camera path may
        see it during the early-connect window before the display name is
        settled."""
        assert get_camera_profile("N7") is get_camera_profile("P2S")
        assert get_camera_profile("n7") is get_camera_profile("P2S")


class TestOtherRTSPModelsStayOnDefault:
    """Defensive — every other RTSP-capable model must still pick up the
    default profile. Catches refactor regressions where a new override
    accidentally captures a model it shouldn't."""

    @pytest.mark.parametrize(
        "model",
        ["X1", "X1C", "X1E", "X2D", "H2C", "H2D", "H2D Pro", "H2DPRO", "H2S"],
    )
    def test_model_uses_default(self, model):
        assert get_camera_profile(model) is DEFAULT_PROFILE


class TestProfileIsImmutable:
    """``CameraProfile`` is a frozen dataclass — accidentally mutating a
    profile via a returned instance would silently leak across callers."""

    def test_profile_is_frozen(self):
        profile = get_camera_profile("P2S")
        with pytest.raises((AttributeError, TypeError)):
            profile.probesize = 99  # type: ignore[misc]

    def test_default_profile_is_frozen(self):
        with pytest.raises((AttributeError, TypeError)):
            DEFAULT_PROFILE.probesize = 99  # type: ignore[misc]


def test_camera_profile_dataclass_shape():
    """Hand-constructed profile honours every field the registry uses."""
    custom = CameraProfile(
        probesize=42,
        analyzeduration=7,
        rtsp_reconnect_max=5,
        rtsp_reconnect_delay=1.5,
        extra_ffmpeg_input_args=("-flag", "value"),
    )
    assert custom.probesize == 42
    assert custom.analyzeduration == 7
    assert custom.rtsp_reconnect_max == 5
    assert custom.rtsp_reconnect_delay == 1.5
    assert custom.extra_ffmpeg_input_args == ("-flag", "value")
