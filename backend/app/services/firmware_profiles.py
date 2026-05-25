"""Per-printer-model firmware apply-capability registry.

Mirrors ``ftp_profiles`` / ``camera_profiles``: a small registry so we don't
sprinkle ``if model == "X"`` through the batch service. **Default = manual
apply** — remote apply is opt-in per model (Phase 2, only after the model's OTA
command is reverse-engineered and confirmed against BambuStudio's DeviceManager).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FirmwareProfile:
    # Whether we can apply firmware remotely over MQTT (Phase 2). Conservative
    # default False → SD upload + a model-specific manual instruction.
    remote_apply: bool = False
    # i18n key for the on-screen "now apply it like this" instruction shown when
    # remote_apply is False (or as a fallback if a remote apply could not be
    # confirmed).
    manual_apply_instruction_key: str = "firmware.manualApply.generic"
    # MQTT state field whose value confirms the new firmware took (Phase 2).
    applied_confirm_via: str | None = None


DEFAULT_PROFILE = FirmwareProfile()

# Phase 1 ships everything on manual instructions. Models gain remote_apply=True
# in Phase 2 once their OTA command is confirmed. Keys are uppercase display
# names AFTER alias normalisation.
_PROFILES: dict[str, FirmwareProfile] = {
    "P1S": FirmwareProfile(manual_apply_instruction_key="firmware.manualApply.p1"),
    "P1P": FirmwareProfile(manual_apply_instruction_key="firmware.manualApply.p1"),
    "X1C": FirmwareProfile(manual_apply_instruction_key="firmware.manualApply.x1"),
    "X1": FirmwareProfile(manual_apply_instruction_key="firmware.manualApply.x1"),
    "X1E": FirmwareProfile(manual_apply_instruction_key="firmware.manualApply.x1"),
    "A1": FirmwareProfile(manual_apply_instruction_key="firmware.manualApply.a1"),
    "A1 MINI": FirmwareProfile(manual_apply_instruction_key="firmware.manualApply.a1"),
    "H2D": FirmwareProfile(manual_apply_instruction_key="firmware.manualApply.h2"),
    "H2C": FirmwareProfile(manual_apply_instruction_key="firmware.manualApply.h2"),
    "H2S": FirmwareProfile(manual_apply_instruction_key="firmware.manualApply.h2"),
    "P2S": FirmwareProfile(manual_apply_instruction_key="firmware.manualApply.generic"),
}

# SSDP internal codes that resolve to a display-name profile. Only codes with an
# unambiguous mapping are listed — mirror ftp_profiles (N7 = P2S). Unknown codes
# fall back to DEFAULT_PROFILE (manual), which is always safe.
_MODEL_ALIASES: dict[str, str] = {
    "N7": "P2S",
}


def get_firmware_profile(model: str | None) -> FirmwareProfile:
    """Return the :class:`FirmwareProfile` for *model*, or the manual default.

    ``model`` may be a display name (``"P2S"``) or an SSDP code (``"N7"``).
    Unknown / missing models fall back to :data:`DEFAULT_PROFILE` (manual apply),
    so the firmware path is never blocked on a missing entry.
    """
    if not model:
        return DEFAULT_PROFILE
    key = model.upper().strip()
    key = _MODEL_ALIASES.get(key, key)
    return _PROFILES.get(key, DEFAULT_PROFILE)
