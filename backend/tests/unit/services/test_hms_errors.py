"""Tests for HMS error code translations."""

from backend.app.services.hms_errors import (
    HMS_ERROR_DESCRIPTIONS,
    PAUSE_REASON_CODES,
    PAUSE_REASON_LABELS,
    classify_pause_reason,
    get_error_description,
)


class TestHMSErrorDescriptions:
    """Tests for the HMS error descriptions dictionary."""

    def test_dictionary_is_not_empty(self):
        """Verify the error descriptions dictionary has entries."""
        assert len(HMS_ERROR_DESCRIPTIONS) > 0

    def test_dictionary_has_expected_count(self):
        """Verify we have the expected number of error codes."""
        # Should have 853 error codes from the frontend
        assert len(HMS_ERROR_DESCRIPTIONS) == 853

    def test_all_keys_are_valid_format(self):
        """Verify all keys follow the XXXX_YYYY format."""
        import re

        pattern = re.compile(r"^[0-9A-F]{4}_[0-9A-F]{4}$")
        for code in HMS_ERROR_DESCRIPTIONS:
            assert pattern.match(code), f"Invalid error code format: {code}"

    def test_all_values_are_non_empty_strings(self):
        """Verify all descriptions are non-empty strings."""
        for code, description in HMS_ERROR_DESCRIPTIONS.items():
            assert isinstance(description, str), f"Description for {code} is not a string"
            assert len(description) > 0, f"Description for {code} is empty"


class TestGetErrorDescription:
    """Tests for the get_error_description function."""

    def test_returns_description_for_known_code(self):
        """Verify known error codes return their descriptions."""
        # 0300_400C = "The task was canceled."
        result = get_error_description("0300_400C")
        assert result == "The task was canceled."

    def test_returns_description_for_ams_error(self):
        """Verify AMS error codes return their descriptions."""
        # 0700_8010 = AMS assist motor overloaded
        result = get_error_description("0700_8010")
        assert "AMS assist motor" in result

    def test_returns_none_for_unknown_code(self):
        """Verify unknown error codes return None."""
        result = get_error_description("XXXX_YYYY")
        assert result is None

    def test_handles_lowercase_input(self):
        """Verify function handles lowercase input."""
        result = get_error_description("0300_400c")
        assert result == "The task was canceled."

    def test_handles_mixed_case_input(self):
        """Verify function handles mixed case input."""
        result = get_error_description("0300_400C")
        assert result == "The task was canceled."

    def test_common_error_codes_have_descriptions(self):
        """Verify common error codes have descriptions."""
        common_codes = [
            "0300_4000",  # Z axis homing failed
            "0300_4006",  # Nozzle clogged
            "0300_8004",  # Filament ran out
            "0500_4001",  # Failed to connect to Bambu Cloud
            "0700_8010",  # AMS assist motor overloaded
        ]
        for code in common_codes:
            result = get_error_description(code)
            assert result is not None, f"Missing description for common code: {code}"


class TestClassifyPauseReason:
    """Tests for ``classify_pause_reason`` — drives the {reason}/{reason_code}/{hms_code}
    template variables on ``print_paused`` notifications + the WS frame payload.

    The function's contract:
      * ``expected_reason`` (planted by internal pause-trigger paths like
        plate-detect) wins over HMS classification — Bambu firmware fires
        HMS ``0300_8001`` ("paused by user") for any pause command we send,
        so without the hint plate-detect-pauses would mislabel as
        user-initiated.
      * Otherwise it walks ``hms_codes`` looking for the first known mapping
        in ``PAUSE_REASON_CODES``; matched HMS gets its precise description
        from ``HMS_ERROR_DESCRIPTIONS`` (more informative than the generic
        ``PAUSE_REASON_LABELS`` copy).
      * Unknown HMS code → ``("hms_other", first-code-description, first-code)``
        so operators can search for the code instead of getting "Unknown".
      * No HMS, no hint → ``("unknown", ...)``.
    """

    def test_no_hms_no_hint_returns_unknown(self):
        code, label, hms = classify_pause_reason(None, None)
        assert code == "unknown"
        assert label == PAUSE_REASON_LABELS["unknown"]
        assert hms is None

    def test_empty_hms_list_returns_unknown(self):
        # Empty list (not None) — same fallback path.
        code, label, hms = classify_pause_reason([], None)
        assert code == "unknown"
        assert hms is None

    def test_expected_reason_wins_over_hms_user_code(self):
        # plate-detect issues client.pause_print() → firmware fires
        # 0300_8001 ("paused by user"). Without the hint we'd mis-classify.
        code, label, hms = classify_pause_reason(["0300_8001"], "plate_objects")
        assert code == "plate_objects"
        assert label == PAUSE_REASON_LABELS["plate_objects"]
        # When expected_reason wins, hms_code is None — the HMS payload is
        # discarded as misleading (it's the firmware's response to OUR
        # command, not the trigger cause).
        assert hms is None

    def test_unknown_expected_reason_falls_through_to_hms(self):
        # A reason key not in PAUSE_REASON_LABELS shouldn't override —
        # safer to fall through to HMS classification than label with a
        # garbage key the frontend can't render.
        code, label, hms = classify_pause_reason(["0300_8001"], "garbage_key")
        assert code == "user"
        assert hms == "0300_8001"

    def test_hms_user_pause(self):
        code, label, hms = classify_pause_reason(["0300_8001"], None)
        assert code == "user"
        assert hms == "0300_8001"
        # Precise HMS description wins over generic label.
        assert "user" in label.lower()
        assert label == HMS_ERROR_DESCRIPTIONS["0300_8001"]

    def test_hms_filament_runout_variants(self):
        # All four filament-runout codes collapse into one normalised key.
        for variant in ["0300_8004", "0300_8015", "07FE_8030", "07FF_8030"]:
            code, label, hms = classify_pause_reason([variant], None)
            assert code == "filament_runout", f"{variant} should classify as filament_runout"
            assert hms == variant
            assert "filament" in label.lower() or "spool" in label.lower()

    def test_hms_door_open_variants(self):
        for variant in ["0300_800F", "0300_8042", "0300_804B"]:
            code, label, hms = classify_pause_reason([variant], None)
            assert code == "door_open", f"{variant} should classify as door_open"
            assert hms == variant

    def test_hms_file_pause_command(self):
        code, label, hms = classify_pause_reason(["0300_8013"], None)
        assert code == "file_pause_command"
        assert hms == "0300_8013"

    def test_hms_ai_first_layer_defect(self):
        code, label, hms = classify_pause_reason(["0300_8002"], None)
        assert code == "ai_first_layer_defect"
        assert hms == "0300_8002"

    def test_hms_ai_spaghetti_variants(self):
        # Both spaghetti-detection + pile-up share the ai_spaghetti key.
        for variant in ["0300_8003", "0300_800A"]:
            code, label, hms = classify_pause_reason([variant], None)
            assert code == "ai_spaghetti", f"{variant} should classify as ai_spaghetti"
            assert hms == variant

    def test_hms_foreign_object(self):
        code, label, hms = classify_pause_reason(["0300_8017"], None)
        assert code == "foreign_object"
        assert hms == "0300_8017"

    def test_hms_presence_check(self):
        code, label, hms = classify_pause_reason(["0500_8089"], None)
        assert code == "presence_check"
        assert hms == "0500_8089"

    def test_unknown_hms_falls_back_to_hms_other(self):
        # Unknown HMS code that exists in HMS_ERROR_DESCRIPTIONS but isn't
        # in PAUSE_REASON_CODES → "hms_other" + the description so operators
        # see what the printer reported.
        code, label, hms = classify_pause_reason(["0300_4000"], None)
        assert code == "hms_other"
        assert hms == "0300_4000"
        assert label == HMS_ERROR_DESCRIPTIONS["0300_4000"]

    def test_completely_unknown_hms_uses_generic_label(self):
        # Code not in HMS_ERROR_DESCRIPTIONS at all → still "hms_other"
        # but with the generic PAUSE_REASON_LABELS fallback so we don't
        # crash on null description.
        code, label, hms = classify_pause_reason(["FFFF_FFFF"], None)
        assert code == "hms_other"
        assert hms == "FFFF_FFFF"
        assert label == PAUSE_REASON_LABELS["hms_other"]

    def test_first_known_hms_wins_over_later_codes(self):
        # When multiple HMS codes are active, the FIRST known mapping wins.
        # Tests the iteration order — door_open is at index 0, filament at 1.
        code, label, hms = classify_pause_reason(["0300_800F", "0300_8004"], None)
        assert code == "door_open"
        assert hms == "0300_800F"

    def test_lowercase_hms_normalised(self):
        # State.hms_errors codes might come in lowercase from MQTT (json
        # payload variability). Function normalises with .upper() before
        # lookup so both work.
        code, label, hms = classify_pause_reason(["0300_8001".lower()], None)
        assert code == "user"
        # Returned hms is upper-cased.
        assert hms == "0300_8001"

    def test_all_pause_reason_codes_have_labels(self):
        # Sanity-check the catalog: every key in PAUSE_REASON_CODES values
        # must exist in PAUSE_REASON_LABELS, otherwise classify_pause_reason
        # would return KeyError when computing the fallback label.
        for hms_code, reason_key in PAUSE_REASON_CODES.items():
            assert reason_key in PAUSE_REASON_LABELS, (
                f"{hms_code} maps to {reason_key!r} which has no entry in PAUSE_REASON_LABELS"
            )

    def test_required_internal_keys_have_labels(self):
        # Internal pause-trigger paths plant these reason keys via
        # set_expected_pause_reason(). They must be in PAUSE_REASON_LABELS
        # so the test_expected_reason_wins_over_hms_user_code path doesn't
        # trip. Keep this list in sync with the runtime callers.
        for required_key in ("plate_objects", "user", "unknown", "hms_other"):
            assert required_key in PAUSE_REASON_LABELS
