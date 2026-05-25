"""Tests for the per-model firmware apply-capability registry."""

from backend.app.services.firmware_profiles import get_firmware_profile


def test_unknown_model_defaults_to_manual_apply():
    p = get_firmware_profile("SomeNewPrinter")
    assert p.remote_apply is False
    assert p.manual_apply_instruction_key  # non-empty i18n key


def test_alias_resolves_to_display_model():
    # N7 is the P2S SSDP code; both resolve to the same profile.
    assert get_firmware_profile("N7") == get_firmware_profile("P2S")


def test_none_model_is_safe():
    assert get_firmware_profile(None).remote_apply is False


def test_known_model_has_specific_instruction():
    # P1S maps to the P1-family manual instruction (not the generic one).
    assert get_firmware_profile("P1S").manual_apply_instruction_key == "firmware.manualApply.p1"
