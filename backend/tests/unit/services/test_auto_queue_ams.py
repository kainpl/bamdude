"""Unit tests for ``services/auto_queue_ams.py``.

Pure-function coverage for the AMS-matching logic ported from upstream:
color helpers, build_loaded_filaments, and match_filaments_to_slots.
The async DB / 3MF / printer_manager code paths
(``get_filament_requirements``, ``compute_ams_mapping_for_printer``)
are exercised in integration tests once the scheduler is in place.
"""

from types import SimpleNamespace

from backend.app.services.auto_queue_ams import (
    _colors_are_similar,
    _normalize_color,
    _normalize_color_for_compare,
    build_loaded_filaments,
    match_filaments_to_slots,
)


class TestColorHelpers:
    def test_normalize_color_strips_hash_and_pads(self) -> None:
        assert _normalize_color("#FF0000") == "#FF0000"
        assert _normalize_color("FF0000") == "#FF0000"
        assert _normalize_color("#ff00ff80") == "#ff00ff"  # alpha trimmed
        assert _normalize_color("") == "#808080"
        assert _normalize_color(None) == "#808080"

    def test_normalize_for_compare_lowercases_no_hash(self) -> None:
        assert _normalize_color_for_compare("#FF0000") == "ff0000"
        assert _normalize_color_for_compare("00FF00") == "00ff00"
        assert _normalize_color_for_compare("") == ""
        assert _normalize_color_for_compare(None) == ""

    def test_colors_are_similar_threshold(self) -> None:
        # Same color → similar
        assert _colors_are_similar("#FF0000", "#FF0000") is True
        # Within default threshold (40)
        assert _colors_are_similar("#FF0000", "#E0_0000".replace("_", "")) is True  # FF vs E0 = 31
        # Outside default threshold
        assert _colors_are_similar("#FF0000", "#0000FF") is False
        assert _colors_are_similar("#FFFFFF", "#000000") is False
        # Empty / malformed → not similar
        assert _colors_are_similar("", "#FF0000") is False
        assert _colors_are_similar("#XX", "#FF0000") is False


def _make_status(ams: list[dict] | None = None, vt_tray: list[dict] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        raw_data={
            "ams": ams or [],
            "vt_tray": vt_tray or [],
            "ams_extruder_map": {},
        }
    )


class TestBuildLoadedFilaments:
    def test_single_ams_unit(self) -> None:
        status = _make_status(
            ams=[
                {
                    "id": 0,
                    "tray": [
                        {"id": 0, "tray_type": "PLA", "tray_color": "#FFFFFF", "tray_info_idx": "GFA00", "remain": 80},
                        {"id": 1, "tray_type": "PETG", "tray_color": "#FF0000", "tray_info_idx": "GFG00", "remain": 50},
                    ],
                }
            ]
        )
        loaded = build_loaded_filaments(status)
        assert len(loaded) == 2
        assert loaded[0]["global_tray_id"] == 0  # ams 0, tray 0
        assert loaded[1]["global_tray_id"] == 1  # ams 0, tray 1
        assert loaded[0]["type"] == "PLA"
        assert loaded[1]["color"] == "#FF0000"
        assert loaded[0]["is_ht"] is False  # 4-tray unit
        assert loaded[0]["is_external"] is False

    def test_multiple_ams_units_global_tray_id(self) -> None:
        """ams 1 → global ids 4-7; ams 2 → 8-11."""
        status = _make_status(
            ams=[
                {"id": 1, "tray": [{"id": 0, "tray_type": "PLA", "tray_color": "#FFF"}]},
                {"id": 2, "tray": [{"id": 3, "tray_type": "PETG", "tray_color": "#F00"}]},
            ]
        )
        loaded = build_loaded_filaments(status)
        assert loaded[0]["global_tray_id"] == 4  # ams 1 * 4 + 0
        assert loaded[1]["global_tray_id"] == 11  # ams 2 * 4 + 3

    def test_ams_ht_single_tray(self) -> None:
        """AMS-HT: id >= 128, single tray, global_tray_id == ams_id."""
        status = _make_status(ams=[{"id": 128, "tray": [{"id": 0, "tray_type": "PA", "tray_color": "#000"}]}])
        loaded = build_loaded_filaments(status)
        assert loaded[0]["is_ht"] is True
        assert loaded[0]["global_tray_id"] == 128

    def test_external_vt_tray(self) -> None:
        status = _make_status(vt_tray=[{"id": 254, "tray_type": "TPU", "tray_color": "#0F0", "remain": 100}])
        loaded = build_loaded_filaments(status)
        assert len(loaded) == 1
        assert loaded[0]["is_external"] is True
        assert loaded[0]["ams_id"] == -1
        assert loaded[0]["global_tray_id"] == 254

    def test_skips_empty_tray_type(self) -> None:
        status = _make_status(ams=[{"id": 0, "tray": [{"id": 0, "tray_type": ""}]}])
        assert build_loaded_filaments(status) == []


class TestMatchFilamentsToSlots:
    def test_unique_tray_info_idx_wins(self) -> None:
        """Even with wrong color, unique tray_info_idx is the match."""
        loaded = [
            {"global_tray_id": 0, "type": "PLA", "color": "#0000FF", "tray_info_idx": "GFA00", "remain": 80},
            {"global_tray_id": 1, "type": "PLA", "color": "#FF0000", "tray_info_idx": "GFA01", "remain": 80},
        ]
        required = [{"slot_id": 1, "type": "PLA", "color": "#FF0000", "tray_info_idx": "GFA00"}]
        mapping = match_filaments_to_slots(required, loaded)
        assert mapping == [0]  # tray_info_idx wins despite wrong color

    def test_exact_color_when_idx_not_unique(self) -> None:
        loaded = [
            {"global_tray_id": 0, "type": "PLA", "color": "#0000FF", "tray_info_idx": "GFA00", "remain": 80},
            {"global_tray_id": 1, "type": "PLA", "color": "#FF0000", "tray_info_idx": "GFA00", "remain": 80},
        ]
        required = [{"slot_id": 1, "type": "PLA", "color": "#FF0000", "tray_info_idx": "GFA00"}]
        mapping = match_filaments_to_slots(required, loaded)
        assert mapping == [1]  # exact color wins among equal idx

    def test_falls_back_to_type_only(self) -> None:
        loaded = [{"global_tray_id": 0, "type": "PLA", "color": "#000000", "tray_info_idx": "", "remain": 80}]
        required = [{"slot_id": 1, "type": "PLA", "color": "#FFFFFF", "tray_info_idx": ""}]
        mapping = match_filaments_to_slots(required, loaded)
        assert mapping == [0]  # type-only fallback

    def test_no_match_returns_minus_one(self) -> None:
        loaded = [{"global_tray_id": 0, "type": "PLA", "color": "#FFF", "tray_info_idx": "", "remain": 80}]
        required = [{"slot_id": 1, "type": "PETG", "color": "#FFF", "tray_info_idx": ""}]
        mapping = match_filaments_to_slots(required, loaded)
        assert mapping == [-1]  # type mismatch → no match

    def test_no_double_assignment(self) -> None:
        """One tray cannot satisfy two slots."""
        loaded = [{"global_tray_id": 0, "type": "PLA", "color": "#FFFFFF", "tray_info_idx": "", "remain": 80}]
        required = [
            {"slot_id": 1, "type": "PLA", "color": "#FFFFFF", "tray_info_idx": ""},
            {"slot_id": 2, "type": "PLA", "color": "#FFFFFF", "tray_info_idx": ""},
        ]
        mapping = match_filaments_to_slots(required, loaded)
        assert mapping == [0, -1]  # second slot can't grab the same tray

    def test_prefer_lowest_picks_least_remain(self) -> None:
        loaded = [
            {"global_tray_id": 0, "type": "PLA", "color": "#FFF", "tray_info_idx": "", "remain": 80},
            {"global_tray_id": 1, "type": "PLA", "color": "#FFF", "tray_info_idx": "", "remain": 20},
        ]
        required = [{"slot_id": 1, "type": "PLA", "color": "#FFF", "tray_info_idx": ""}]
        mapping = match_filaments_to_slots(required, loaded, prefer_lowest=True)
        assert mapping == [1]  # lowest remain wins

    def test_nozzle_filter_hard_constraint(self) -> None:
        """Cross-nozzle assignment must NOT happen."""
        loaded = [
            {"global_tray_id": 0, "type": "PLA", "color": "#FFF", "tray_info_idx": "", "extruder_id": 0, "remain": 80},
            {"global_tray_id": 1, "type": "PLA", "color": "#FFF", "tray_info_idx": "", "extruder_id": 1, "remain": 80},
        ]
        required = [{"slot_id": 1, "type": "PLA", "color": "#FFF", "tray_info_idx": "", "nozzle_id": 1}]
        mapping = match_filaments_to_slots(required, loaded)
        assert mapping == [1]  # extruder_id 1 only

    def test_empty_required_returns_none(self) -> None:
        assert match_filaments_to_slots([], []) is None
