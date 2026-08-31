"""Pure-logic tests for the mid-print tray-split math (#1793).

The helper lives in ``backend/app/utils/tray_split.py`` and is exercised by
both inventory backends (``usage_tracker`` and ``spoolman_tracking``). These
tests pin the algorithm so a change in one caller can't silently break the
other — cross-inventory parity is a HARD RULE for this project.
"""

from __future__ import annotations

from backend.app.utils.tray_split import compute_tray_split_grams


class TestComputeTraySplitGrams:
    """Segment-attribution algorithm — gcode preferred, linear fallback, equal split."""

    def test_empty_tray_changes_returns_empty(self):
        assert (
            compute_tray_split_grams(
                tray_changes=[],
                total_weight=100.0,
                slot_id=1,
                layer_usage=None,
                density=1.24,
                diameter=1.75,
                total_layers=200,
                last_layer_num=200,
            )
            == []
        )

    def test_single_segment_charges_everything_to_that_tray(self):
        segments = compute_tray_split_grams(
            tray_changes=[(0, 0)],
            total_weight=72.56,
            slot_id=1,
            layer_usage=None,
            density=1.24,
            diameter=1.75,
            total_layers=100,
            last_layer_num=100,
        )
        assert segments == [(0, 0, 72.56)]

    def test_two_segments_linear_split_by_layer_ratio(self):
        # Runout at layer 37 of 100 total; no gcode available → linear.
        # Segment 0 (tray 0, layers 0-37) = 100 * 37/100 = 37g
        # Segment 1 (tray 1, layers 37-end) = 100 - 37 = 63g (remainder)
        segments = compute_tray_split_grams(
            tray_changes=[(0, 0), (1, 37)],
            total_weight=100.0,
            slot_id=1,
            layer_usage=None,
            density=1.24,
            diameter=1.75,
            total_layers=100,
            last_layer_num=100,
        )
        assert segments == [(0, 0, 37.0), (1, 1, 63.0)]

    def test_two_segments_gcode_preferred_over_linear(self):
        # layer_usage stores mm of filament extruded per (layer, filament_id).
        # Values are cumulative-per-key inside get_cumulative_usage_at_layer.
        # 20 layers, filament_id=0 (slot_id=1 → filament_id 0):
        #   layer 10 → 100mm cumulative
        #   layer 20 → 300mm cumulative
        # tray change at layer 10 → seg 0 spans layers 0-10 (mm 0 → 100),
        #                            seg 1 spans layers 10-end.
        # mm_to_grams(100, 1.75, 1.24) ≈ 0.298g; last segment absorbs the rest.
        layer_usage = {
            5: {0: 50.0},
            10: {0: 100.0},
            15: {0: 200.0},
            20: {0: 300.0},
        }
        segments = compute_tray_split_grams(
            tray_changes=[(0, 0), (1, 10)],
            total_weight=1.0,  # sentinel — we assert the seg1 remainder
            slot_id=1,
            layer_usage=layer_usage,
            density=1.24,
            diameter=1.75,
            total_layers=20,
            last_layer_num=20,
        )
        # Seg 0 charged from gcode delta (mm 0 → 100).
        # Seg 1 gets total_weight - seg0 as remainder.
        assert segments[0][0] == 0
        assert segments[0][1] == 0  # tray 0
        assert segments[0][2] > 0  # non-zero gcode contribution
        assert segments[1][0] == 1
        assert segments[1][1] == 1  # tray 1
        # Sum equals the input total by construction (last segment absorbs).
        assert round(segments[0][2] + segments[1][2], 6) == 1.0

    def test_three_segments_last_absorbs_rounding_drift(self):
        # 100g over three segments at layers 30 and 60 of 90; linear fallback.
        # Seg 0: 100 * 30/90 = 33.3333...
        # Seg 1: 100 * 30/90 = 33.3333...
        # Seg 2: remainder = 100 - 66.6666... = 33.3333... — exact by construction
        segments = compute_tray_split_grams(
            tray_changes=[(0, 0), (1, 30), (2, 60)],
            total_weight=100.0,
            slot_id=1,
            layer_usage=None,
            density=1.24,
            diameter=1.75,
            total_layers=90,
            last_layer_num=90,
        )
        assert len(segments) == 3
        assert round(sum(g for _, _, g in segments), 6) == 100.0
        assert segments[0][1] == 0
        assert segments[1][1] == 1
        assert segments[2][1] == 2

    def test_no_layer_info_at_all_falls_to_equal_split(self):
        # Denominator 0 → last-resort equal-split; last segment absorbs remainder.
        segments = compute_tray_split_grams(
            tray_changes=[(0, 0), (1, 50)],
            total_weight=90.0,
            slot_id=1,
            layer_usage=None,
            density=1.24,
            diameter=1.75,
            total_layers=0,
            last_layer_num=0,
        )
        # 90g / 2 = 45g each; sum still 90 by remainder mechanic.
        assert segments == [(0, 0, 45.0), (1, 1, 45.0)]

    def test_last_layer_num_used_when_total_layers_zero(self):
        # P1S firmware-reset scenario: total_layers=0 at completion, but the
        # captured last_layer_num survives. Should give the same linear split as
        # if total_layers had held its value (#1771 cascade).
        segments_captured = compute_tray_split_grams(
            tray_changes=[(0, 0), (1, 30)],
            total_weight=100.0,
            slot_id=1,
            layer_usage=None,
            density=1.24,
            diameter=1.75,
            total_layers=0,
            last_layer_num=100,
        )
        segments_normal = compute_tray_split_grams(
            tray_changes=[(0, 0), (1, 30)],
            total_weight=100.0,
            slot_id=1,
            layer_usage=None,
            density=1.24,
            diameter=1.75,
            total_layers=100,
            last_layer_num=100,
        )
        assert segments_captured == segments_normal

    def test_slot_id_maps_to_zero_based_filament_id_in_gcode(self):
        # slot_id 2 → filament_id 1 in layer_usage. If we mistakenly used
        # slot_id as-is, we'd read filament_id 2 which is absent → 0mm delta →
        # seg 0 gets 0, seg 1 (remainder) gets the whole total. Guard against
        # that regression (the exact #1771 shape).
        layer_usage = {
            5: {0: 0.0, 1: 40.0},
            10: {0: 0.0, 1: 80.0},
            20: {0: 0.0, 1: 160.0},
        }
        segments = compute_tray_split_grams(
            tray_changes=[(0, 0), (1, 10)],
            total_weight=1.0,
            slot_id=2,
            layer_usage=layer_usage,
            density=1.24,
            diameter=1.75,
            total_layers=20,
            last_layer_num=20,
        )
        # Seg 0 gcode delta on filament_id=1 is non-zero → not 0g.
        assert segments[0][2] > 0


class TestCancelledPrintNormalisation:
    """A cancelled print's segments divide what was PRINTED, not the whole plate.

    ``slot_progress_fraction`` is a fraction of the FULL plate ("exact at
    completion"), while a failed print's caller passes an already-shortened
    ``total_weight`` (estimate x progress). Multiplying one by the other applies
    the shortening twice to every segment but the last, which then swallows the
    difference as if it were rounding drift.

    Printer 5, archive 822 (2026-08-31): reel swapped at layer 227, jam at 405,
    cancelled. 259.3 g of a 364.95 g plate. The books read 120.0 g for the first
    reel and 139.3 g for the second when the gcode timeline says 169 / 90 — the
    total was right, ~49 g sat on the wrong reel.
    """

    # Cumulative mm for filament 0 across a 570-layer plate; the print stopped
    # at 405 having laid down 70.92% of the slot's own timeline.
    LAYER_USAGE = {
        0: {0: 0.0},
        227: {0: 462.8},
        405: {0: 709.2},
        570: {0: 1000.0},
    }

    def test_segments_divide_the_printed_portion_not_the_whole_plate(self):
        segments = compute_tray_split_grams(
            tray_changes=[(254, 0), (254, 227)],
            total_weight=259.3,  # 364.95 g estimate x 0.7105 progress
            slot_id=1,
            layer_usage=self.LAYER_USAGE,
            density=1.24,
            diameter=1.75,
            total_layers=570,
            last_layer_num=405,  # the last layer actually printed
        )
        first, second = segments[0][2], segments[1][2]
        # 259.3 * (0.4628 / 0.7092) and the remainder.
        assert round(first, 1) == 169.2
        assert round(second, 1) == 90.1
        assert round(first + second, 1) == 259.3

    def test_completed_print_is_unchanged_by_the_normalisation(self):
        # Printed to the end: the fraction at the last layer is 1.0, so
        # dividing by it is a no-op and the historical numbers stand.
        segments = compute_tray_split_grams(
            tray_changes=[(254, 0), (254, 227)],
            total_weight=364.95,
            slot_id=1,
            layer_usage=self.LAYER_USAGE,
            density=1.24,
            diameter=1.75,
            total_layers=570,
            last_layer_num=570,
        )
        assert round(segments[0][2], 1) == round(364.95 * 0.4628, 1)
        assert round(segments[0][2] + segments[1][2], 2) == 364.95

    def test_unknown_last_layer_keeps_the_old_behaviour(self):
        # last_layer_num 0 means the layer was never captured — there is no
        # printed fraction to normalise by, so the segments must not move.
        segments = compute_tray_split_grams(
            tray_changes=[(254, 0), (254, 227)],
            total_weight=259.3,
            slot_id=1,
            layer_usage=self.LAYER_USAGE,
            density=1.24,
            diameter=1.75,
            total_layers=570,
            last_layer_num=0,
        )
        assert round(segments[0][2], 1) == round(259.3 * 0.4628, 1)
