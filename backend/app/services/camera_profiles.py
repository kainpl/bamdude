"""Per-model camera profile registry.

Some printer models need different ffmpeg / RTSP tuning than the X1 / H2
fast-startup defaults. Adding the next quirky model is a config entry
in ``_PROFILES``, not another global constant scattered through camera.py.

Upstream Bambuddy commit 67cb5275 / #1395 — P2S firmware ``01.02.00.00``
needs a much larger probesize than the default to lock onto its slower
keyframe pacing; the X1/H2 default (``probesize=32``, ``analyzeduration=0``)
makes ffmpeg's stderr say "consider increasing probesize" and give up
after ~2 s.

Pattern is intentionally extensible: profile knobs live in the
``CameraProfile`` dataclass, default values mirror the historical
X1 / H2 fast-startup constants verbatim so every existing model sees
zero behaviour change.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CameraProfile:
    """Per-model RTSP / ffmpeg tuning knobs.

    Defaults match the historical hard-coded values that lived in
    ``backend/app/api/routes/camera.py`` before this registry was lifted
    out, so every printer that doesn't have an explicit profile entry
    sees zero behaviour change.
    """

    # ffmpeg ``-probesize`` — bytes to read from the input before giving
    # up on format detection. X1 / H2 / X2D produce keyframes fast enough
    # that 32 bytes is plenty; P2S's slower keyframe pacing needs ~1 MB.
    probesize: int = 32
    # ffmpeg ``-analyzeduration`` — microseconds spent analysing input.
    # 0 means "skip", which is the X1 / H2 default for fast startup.
    analyzeduration: int = 0
    # Max consecutive RTSP reconnection attempts before giving up.
    rtsp_reconnect_max: int = 30
    # Seconds between RTSP reconnect attempts.
    rtsp_reconnect_delay: float = 0.2
    # Optional extra ``-input``-side ffmpeg flags inserted before ``-i``.
    # Reserved for future per-model quirks; default is none.
    extra_ffmpeg_input_args: tuple[str, ...] = field(default_factory=tuple)


DEFAULT_PROFILE = CameraProfile()


# Display-name → profile registry. Match is case-insensitive after
# trimming. Aliases for SSDP-only internal codes are added in
# ``_MODEL_ALIASES`` below so the camera path resolves correctly during
# the early-connect window before the display name is settled.
_PROFILES: dict[str, CameraProfile] = {
    # P2S: firmware 01.02.00.00 produces a slow first keyframe; ffmpeg
    # needs ~1 MB of probe data to lock onto the format and ~500 ms of
    # analysis time. Sized so that startup latency stays sub-second on
    # a healthy stream while still surviving the slow-keyframe case.
    "P2S": CameraProfile(probesize=1_000_000, analyzeduration=500_000),
}


# SSDP-discovery / internal-code → display-name alias map. Bambu's
# SSDP responses use short codes (``N7`` for P2S, ``N1`` for A1 Mini,
# etc.) and the printer model only resolves to its display name after
# the first push_status comes through. During the early-connect window
# the camera code may see the SSDP code instead of the display name —
# this alias map lets a profile entry under the display name still
# match.
_MODEL_ALIASES: dict[str, str] = {
    "N7": "P2S",
}


def get_camera_profile(model: str | None) -> CameraProfile:
    """Return the ``CameraProfile`` for ``model`` (or ``DEFAULT_PROFILE``).

    Lookup is case-insensitive on the trimmed string and falls back to
    ``DEFAULT_PROFILE`` for unknown models, ``None``, and the empty
    string. SSDP internal codes resolve through ``_MODEL_ALIASES`` so the
    early-connect window before the display name is settled still picks
    the right profile.
    """
    if not model:
        return DEFAULT_PROFILE
    key = model.strip().upper()
    if not key:
        return DEFAULT_PROFILE
    # SSDP code → display name aliasing
    key = _MODEL_ALIASES.get(key, key)
    return _PROFILES.get(key, DEFAULT_PROFILE)


__all__ = ["CameraProfile", "DEFAULT_PROFILE", "get_camera_profile"]
