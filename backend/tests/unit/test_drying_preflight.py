"""One set of checks for manual and scheduled drying; the blocker is chosen by priority (upstream d37ce94f)."""

import pytest

from backend.app.services import drying_preflight as pf
from backend.app.services.printer_manager import first_drying_blocking_reason


def test_power_outranks_whatever_the_firmware_listed_first():
    assert first_drying_blocking_reason({"dry_sf_reason": [3, 8]})[0] == 8
    assert first_drying_blocking_reason({"dry_sf_reason": [2, 3]})[0] == 3
    assert first_drying_blocking_reason({"dry_sf_reason": [99, 2]})[0] == 2
    assert first_drying_blocking_reason({"dry_sf_reason": [99]}) is None


@pytest.mark.parametrize(
    ("codes", "expected"),
    [
        ([1], "blocked_power"),
        ([8, 3], "blocked_power"),
        ([3], "blocked_filament_at_outlet"),
        ([0], "blocked_other"),
        ([], None),
    ],
)
def test_blocker_code(codes, expected):
    assert pf.blocker_code({"dry_sf_reason": codes}) == expected


def test_refusal_code():
    assert pf.refusal_code("P1S", "01.08.00.00") == "screen_only"
    assert pf.refusal_code("A1", "01.04.00.00") == "unsupported"
    assert pf.refusal_code("X1C", None, require_firmware=False) is None


def test_max_temp_for_unit():
    assert pf.max_temp_for_unit({"module_type": "n3s"}) == 85
    assert pf.max_temp_for_unit({"module_type": "n3f"}) == 65
    assert pf.max_temp_for_unit(None) == 65


def test_the_refusal_texts_are_catalogued_sentences():
    """The route raises these as literals (the catalog scanner reads raise sites);
    the scheduled-drying writer raises the constants. One text, translated once."""
    import json
    from pathlib import Path

    catalog = json.loads(
        (Path(__file__).resolve().parents[2] / "app" / "data" / "api_errors_uk.json").read_text(encoding="utf-8")
    )
    assert pf.SCREEN_ONLY_DETAIL in catalog
    assert pf.UNSUPPORTED_DETAIL in catalog


def test_resolve_filament():
    unit = {"tray": [{"tray_type": ""}, {"tray_type": "PETG"}]}
    assert pf.resolve_filament(unit, "") == "PETG"
    assert pf.resolve_filament(unit, "ABS") == "ABS"
    assert pf.resolve_filament(None, "") == "PLA"
