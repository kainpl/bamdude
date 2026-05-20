"""Regression tests for D.7b/D.8 (#1390): ``_map_spoolman_spool`` derives a
synthetic ``weight_used`` and a matching ``weight_used_baseline`` from
Spoolman's two independent ``used_weight`` + ``remaining_weight`` fields,
so the InventorySpool shape returned to the FE behaves identically
whether the spool lives in the internal table or in Spoolman.

The mapping must satisfy two FE invariants:

* ``remaining = label_weight - weight_used`` must equal Spoolman's real
  ``remaining_weight`` (the user's visible "remaining" stays correct).
* ``max(0, weight_used - weight_used_baseline)`` must equal Spoolman's
  real ``used_weight`` (the visible "Total Consumed" counter matches
  internal-mode semantics across the reset boundary).
"""

from __future__ import annotations

from backend.app.api.routes._spoolman_helpers import _map_spoolman_spool


def _base_spool(*, used_weight: float, remaining_weight: float | None) -> dict:
    spool: dict = {
        "id": 7,
        "filament": {
            "id": 99,
            "name": "PLA Black",
            "material": "PLA",
            "color_hex": "000000",
            "weight": 1000,
            "vendor": {"name": "Bambu Lab"},
        },
        "extra": {},
        "registered": "2026-01-01T00:00:00Z",
        "used_weight": used_weight,
    }
    if remaining_weight is not None:
        spool["remaining_weight"] = remaining_weight
    return spool


class TestSyntheticWeightUsed:
    def test_pre_reset_consumed_456_remaining_544(self):
        """Spoolman: used_weight=456, remaining_weight=544 (typical post-print
        state) → FE sees weight_used=456, baseline=0, so consumed=456 and
        remaining = label - weight_used = 544."""
        mapped = _map_spoolman_spool(_base_spool(used_weight=456.0, remaining_weight=544.0))
        assert mapped["weight_used"] == 456.0
        assert mapped["weight_used_baseline"] == 0.0
        # Visible consumed = weight_used - baseline = 456
        assert (mapped["weight_used"] or 0) - (mapped["weight_used_baseline"] or 0) == 456.0
        # Visible remaining = label - weight_used = 544
        assert mapped["label_weight"] - (mapped["weight_used"] or 0) == 544.0

    def test_post_reset_consumed_0_remaining_544(self):
        """After "Reset usage to 0" on Spoolman: used_weight=0,
        remaining_weight=544 (preserved). FE must see consumed=0 AND
        remaining=544 — the synthetic mapping is what guarantees this."""
        mapped = _map_spoolman_spool(_base_spool(used_weight=0.0, remaining_weight=544.0))
        assert mapped["weight_used"] == 456.0, "weight_used must mirror label - remaining (544 less than 1000)"
        assert mapped["weight_used_baseline"] == 456.0, "baseline absorbs the reset so consumed = 0"
        # Visible consumed = weight_used - baseline = 0
        assert (mapped["weight_used"] or 0) - (mapped["weight_used_baseline"] or 0) == 0
        # Visible remaining = label - weight_used = 544 (unchanged across the reset)
        assert mapped["label_weight"] - (mapped["weight_used"] or 0) == 544.0

    def test_missing_remaining_weight_falls_back_to_used_weight(self):
        """Spoolman spool with no remaining_weight set → fall back to the
        legacy mapping (weight_used = used_weight, baseline = 0)."""
        mapped = _map_spoolman_spool(_base_spool(used_weight=123.0, remaining_weight=None))
        assert mapped["weight_used"] == 123.0
        assert mapped["weight_used_baseline"] == 0.0

    def test_fully_consumed_spool(self):
        """used_weight=1000, remaining_weight=0 → consumed=1000, baseline=0,
        remaining=0 — sanity that the clamps don't blow up at extremes."""
        mapped = _map_spoolman_spool(_base_spool(used_weight=1000.0, remaining_weight=0.0))
        assert mapped["weight_used"] == 1000.0
        assert mapped["weight_used_baseline"] == 0.0
        assert mapped["label_weight"] - (mapped["weight_used"] or 0) == 0.0

    def test_overconsumed_spool_clamped(self):
        """Spoolman lets remaining_weight go negative; we clamp synthetic
        weight_used to ``[0, +inf)`` so the FE doesn't show negative
        consumed cells."""
        mapped = _map_spoolman_spool(_base_spool(used_weight=1100.0, remaining_weight=-100.0))
        # synthetic = max(0, 1000 - (-100)) = 1100
        assert mapped["weight_used"] == 1100.0
        # baseline = max(0, 1100 - 1100) = 0
        assert mapped["weight_used_baseline"] == 0.0
