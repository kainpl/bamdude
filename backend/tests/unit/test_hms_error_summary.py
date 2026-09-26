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
    code alone is the floor, not the target.

    ⚠️ No model means no catalogue, and never another machine's. BambuStudio
    does the same — `_query_hms_msg` logs "there are no hms info for the
    device" and returns empty. The fix for a model we could not describe was to
    fetch ITS catalogue (`query.php?d=<type>`, which the importer now does for
    every device type Bambu names), not to answer out of someone else's file.
    """
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


# upstream 6988a30e: a fault from the printer's hms[] array carries its alert
# level in the code's high half, and the label came out as 0500_24038 — a code
# nobody can look up, and one that matched no catalogue key, so the sentence
# explaining the failure was dropped with it.


def test_an_hms_array_fault_is_masked_to_its_error_number():
    summary = _format([{"code": "0x24038", "attr": 0x05000000, "module": 0x5, "severity": 2}])
    assert summary is not None
    assert "0500_4038" in summary and "24038" not in summary
    assert "nozzle diameter" in summary.lower()


def test_an_integer_code_is_read_too():
    summary = _format([{"code": 0x24038, "attr": 0x05000000, "module": 0x5, "severity": 2}])
    assert summary is not None and "0500_4038" in summary


def test_the_completion_payload_carries_the_full_code():
    # The exact 16-character key is what the catalogue lookup tries first; the
    # completion payload the failure reason is built from used to drop it.
    import inspect
    import re

    from backend.app.services import bambu_mqtt

    source = re.sub(r"\s+", " ", inspect.getsource(bambu_mqtt))
    assert '"severity": e.severity, "full_code": e.full_code' in source
