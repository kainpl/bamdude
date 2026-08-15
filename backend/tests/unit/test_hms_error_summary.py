"""Tests for `main._format_hms_error_summary` — the helper that turns MQTT
``hms_errors`` into a human-readable ``PrintQueueItem.error_message`` on
pre-print failures (#1111).

Ports upstream Bambuddy's `test_hms_error_summary.py` (audit cycle
v0.2.3.2 → v0.2.4b1, item A.6).
"""


def _format(hms_errors):
    from backend.app.main import _format_hms_error_summary

    # ⚠️ The model is required for the text now: descriptions are per machine,
    # and 325 codes read differently between an X2D and an X1C alone. Without
    # one the summary still forms — just as the bare code, covered below.
    return _format_hms_error_summary(hms_errors, "20P")


def test_returns_none_for_empty_list():
    assert _format([]) is None
    assert _format(None or []) is None


def test_formats_known_nozzle_mismatch_code():
    """0500_4038 is the nozzle-size-mismatch code from the HMS table — the
    common trigger for #1111."""
    summary = _format([{"code": "0x4038", "attr": 0x05000000, "module": 0x5, "severity": 1}])
    assert summary is not None
    assert "0500_4038" in summary
    assert "nozzle diameter" in summary.lower()


def test_formats_unknown_code_as_bare_short_code():
    summary = _format([{"code": "0x9999", "attr": 0x99990000, "module": 0x99, "severity": 1}])
    assert summary == "[9999_9999]"


def test_without_a_model_the_code_still_reaches_the_operator():
    """A failure reason of "[0500_4038]" is poor; a blank one is useless. The
    code alone is the floor, not the target."""
    from backend.app.main import _format_hms_error_summary

    summary = _format_hms_error_summary([{"code": "0x4038", "attr": 0x05000000, "module": 0x5, "severity": 1}])
    assert summary == "[0500_4038]"


def test_joins_multiple_errors_with_semicolons():
    summary = _format(
        [
            {"code": "0x4038", "attr": 0x05000000, "module": 0x5, "severity": 1},
            {"code": "0x9999", "attr": 0x99990000, "module": 0x99, "severity": 1},
        ]
    )
    assert summary is not None
    assert "; " in summary
    assert summary.count("[") == 2


def test_tolerates_malformed_entry_and_skips_it():
    summary = _format(
        [
            {"code": "not-hex", "attr": "also-not-int"},
            {"code": "0x4038", "attr": 0x05000000, "module": 0x5, "severity": 1},
        ]
    )
    assert summary is not None
    assert "0500_4038" in summary


def test_all_malformed_returns_none():
    assert _format([{"code": "not-hex", "attr": "also-not-int"}]) is None
