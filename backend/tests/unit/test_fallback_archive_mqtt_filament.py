"""Tests for _extract_filament_data_from_mqtt (#1533 + #1533 follow-up).

BamDude adaptation note: our ``PrintArchive.filament_color`` column is
``String(50)`` (upstream's is 200), so the helper truncates colours to 50 —
these tests pin that tighter limit.
"""

from backend.app.main import _extract_filament_data_from_mqtt


def _ams_unit(unit_id: int, trays: list[dict]) -> dict:
    return {"id": unit_id, "tray": trays}


def _tray(tray_id: int, ttype: str | None, color: str | None) -> dict:
    out: dict = {"id": tray_id}
    if ttype is not None:
        out["tray_type"] = ttype
    if color is not None:
        out["tray_color"] = color
    return out


class TestExtractFilamentDataFromMqtt:
    def test_empty_payload_returns_empty_dict(self):
        assert _extract_filament_data_from_mqtt({}) == {}
        assert _extract_filament_data_from_mqtt({"ams": None}) == {}
        assert _extract_filament_data_from_mqtt({"ams": {}}) == {}
        assert _extract_filament_data_from_mqtt({"ams": {"ams": []}}) == {}

    def test_no_loaded_slots_returns_empty(self):
        data = {"ams": {"ams": [_ams_unit(0, [_tray(0, None, None), _tray(1, "", "")])]}}
        assert _extract_filament_data_from_mqtt(data) == {}

    def test_no_mapping_lists_all_loaded_slots_sorted(self):
        data = {
            "ams": {
                "ams": [
                    _ams_unit(
                        0,
                        [
                            _tray(0, "PLA", "FF0000"),
                            _tray(1, "PETG", "00FF00"),
                            _tray(2, "ABS", "0000FF"),
                        ],
                    ),
                ],
            },
        }
        result = _extract_filament_data_from_mqtt(data)
        assert result == {"filament_type": "PLA,PETG,ABS", "filament_color": "FF0000,00FF00,0000FF"}

    def test_ams_mapping_narrows_to_used_slots(self):
        data = {
            "ams": {
                "ams": [
                    _ams_unit(
                        0,
                        [
                            _tray(0, "PLA", "FF0000"),
                            _tray(1, "PETG", "00FF00"),
                            _tray(2, "ABS", "0000FF"),
                            _tray(3, "TPU", "FFFF00"),
                        ],
                    ),
                ],
            },
        }
        result = _extract_filament_data_from_mqtt(data, ams_mapping=[3, 0, 1])
        assert result == {"filament_type": "TPU,PLA,PETG", "filament_color": "FFFF00,FF0000,00FF00"}

    def test_ams_mapping_with_vt_tray_sentinels_filtered_out(self):
        data = {
            "ams": {
                "ams": [
                    _ams_unit(
                        0,
                        [
                            _tray(0, "PLA", "FF0000"),
                            _tray(1, "PETG", "00FF00"),
                        ],
                    ),
                ],
            },
        }
        result = _extract_filament_data_from_mqtt(data, ams_mapping=[-1, 0, 1])
        assert result == {"filament_type": "PLA,PETG", "filament_color": "FF0000,00FF00"}

    def test_dual_ams_global_ids_use_unit4_offset(self):
        data = {
            "ams": {
                "ams": [
                    _ams_unit(0, [_tray(0, "PLA", "FF0000")]),
                    _ams_unit(1, [_tray(0, "PETG-CF", "112233")]),
                ],
            },
        }
        result = _extract_filament_data_from_mqtt(data, ams_mapping=[4, 0])
        assert result == {"filament_type": "PETG-CF,PLA", "filament_color": "112233,FF0000"}

    def test_mapping_pointing_at_unknown_slot_falls_through_to_known_only(self):
        data = {"ams": {"ams": [_ams_unit(0, [_tray(0, "PLA", "FF0000")])]}}
        result = _extract_filament_data_from_mqtt(data, ams_mapping=[7, 0])
        assert result == {"filament_type": "PLA", "filament_color": "FF0000"}

    def test_mapping_entirely_unknown_returns_empty(self):
        data = {"ams": {"ams": [_ams_unit(0, [_tray(0, "PLA", "FF0000")])]}}
        assert _extract_filament_data_from_mqtt(data, ams_mapping=[5, 6]) == {}

    def test_color_truncation_at_our_column_limit(self):
        """Our filament_color column is String(50) — long multi-color prints
        must not exceed it (BamDude divergence from upstream's 200)."""
        trays = [_tray(i, "PLA", f"{i:06X}") for i in range(4)]
        data = {"ams": {"ams": [_ams_unit(u, trays) for u in range(8)]}}
        result = _extract_filament_data_from_mqtt(data)
        assert "filament_color" in result
        assert len(result["filament_color"]) <= 50

    def test_type_truncation_at_column_limit(self):
        """filament_type column is String(50). Many filaments must truncate."""
        trays = [_tray(i, "PETG-CF", "AABBCC") for i in range(4)]
        data = {"ams": {"ams": [_ams_unit(u, trays) for u in range(4)]}}
        result = _extract_filament_data_from_mqtt(data)
        assert "filament_type" in result
        assert len(result["filament_type"]) <= 50

    def test_color_missing_only_emits_type(self):
        data = {"ams": {"ams": [_ams_unit(0, [_tray(0, "PLA", None)])]}}
        result = _extract_filament_data_from_mqtt(data)
        assert result == {"filament_type": "PLA"}


class TestCallbackWrapperShape:
    """Regression for the wrapper shape bambu_mqtt hands on_print_start at
    runtime (#1533 follow-up) — the inner-only lookup returned {} for every
    real print, leaving fallback archives' filament fields NULL."""

    def test_callback_wrapper_payload_resolves_raw_data_path(self):
        inner = {"ams": {"ams": [_ams_unit(0, [_tray(0, "PETG", "FFFFFFFF")])]}}
        wrapper = {
            "filename": "/data/Metadata/plate_1.gcode",
            "subtask_name": "xyz-10mm-calibration-cube",
            "remaining_time": 1200,
            "raw_data": inner,
            "ams_mapping": [0],
        }
        result = _extract_filament_data_from_mqtt(wrapper, ams_mapping=[0])
        assert result == {"filament_type": "PETG", "filament_color": "FFFFFFFF"}

    def test_wrapper_with_no_ams_mapping_falls_back_to_all_loaded(self):
        inner = {"ams": {"ams": [_ams_unit(0, [_tray(0, "PLA", "FF0000"), _tray(1, "PETG", "00FF00")])]}}
        wrapper = {"raw_data": inner}
        result = _extract_filament_data_from_mqtt(wrapper)
        assert result == {"filament_type": "PLA,PETG", "filament_color": "FF0000,00FF00"}
