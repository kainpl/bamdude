"""Unit tests for ``services/auto_queue_ams.py``.

What is left of the module after the upstream matcher port was deleted: the
colour helpers ``auto_queue_eligibility`` still reads, and
``build_loaded_filaments`` — the one reader that says what a printer is holding,
overlay seen through. Choosing a tray is ``services/filament_routing``'s job and
is covered by ``unit/services/test_filament_routing.py``.
"""

from types import SimpleNamespace

from backend.app.services.auto_queue_ams import (
    _normalize_color,
    _normalize_color_for_compare,
    build_loaded_filaments,
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

    def test_a_masked_slot_reports_the_actual_spool(self) -> None:
        """Mapping and the low-filament announcement reason about the SPOOL, so
        an advertised profile must not reach either (spec §6.3)."""
        from backend.app.services import ams_advertised_overlay as overlay
        from backend.app.services.ams_advertised_overlay import OverlayEntry

        status = _make_status(
            ams=[
                {"id": 0, "tray": [{"id": 1, "tray_type": "PETG", "tray_color": "000000FF", "tray_info_idx": "GFG99"}]}
            ]
        )
        overlay.replace_printer(
            9, {(0, 1): OverlayEntry("PETG", "FF0000FF", "GFG00", (), "000000FF", "GFG99", "internal")}
        )
        masked = build_loaded_filaments(status, 9)[0]
        assert (masked["color"], masked["tray_info_idx"]) == ("#FF0000", "GFG00")
        # Without the printer id there is nothing to look the slot up by, and
        # an unknown printer has no entries — both report what the printer shows.
        live = build_loaded_filaments(status)[0]
        assert (live["color"], live["tray_info_idx"]) == ("#000000", "GFG99")
        assert build_loaded_filaments(status, 10)[0]["color"] == "#000000"
