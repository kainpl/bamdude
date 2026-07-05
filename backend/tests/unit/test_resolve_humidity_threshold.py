"""Unit tests for PrintScheduler.resolve_humidity_threshold (#1605, G5/E7).

The resolver picks the most-restrictive (lowest) per-filament humidity trigger
across the tray types loaded in one AMS, falling back to a ``default`` key and
finally to the global ``ams_humidity_fair`` value when no overrides exist.
"""

from backend.app.services.print_scheduler import PrintScheduler


def _trays(*types: str) -> list[dict]:
    return [{"tray_type": t} for t in types]


def test_empty_thresholds_returns_global_fallback():
    assert PrintScheduler.resolve_humidity_threshold(_trays("PLA", "ASA"), {}, 60) == 60


def test_no_loaded_trays_returns_default_key():
    # No tray_type on any slot → no per-type constraint → default key wins.
    assert PrintScheduler.resolve_humidity_threshold(_trays("", ""), {"default": 45}, 60) == 45


def test_no_loaded_trays_no_default_falls_back():
    assert PrintScheduler.resolve_humidity_threshold(_trays(), {"PLA": 55}, 60) == 60


def test_most_restrictive_across_mixed_types():
    thresholds = {"default": 60, "PLA": 55, "PA": 10}
    # min(55, 10) — the nylon constraint dominates.
    assert PrintScheduler.resolve_humidity_threshold(_trays("PLA", "PA"), thresholds, 60) == 10


def test_unknown_type_uses_default_key():
    thresholds = {"default": 40, "PLA": 55}
    assert PrintScheduler.resolve_humidity_threshold(_trays("Weird"), thresholds, 60) == 40


def test_base_type_normalization_and_uppercase():
    # "pla matte" → base "PLA"; keys are stored uppercase.
    thresholds = {"PLA": 50}
    assert PrintScheduler.resolve_humidity_threshold(_trays("pla matte"), thresholds, 60) == 50
