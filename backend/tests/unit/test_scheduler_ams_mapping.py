"""What the print scheduler still reads off a printer's live status.

Colour normalisation and ``_build_loaded_filaments`` — the list every remaining
caller (``background_dispatch``, ``filament_preflight``, ``filament_low``) asks
for. The greedy matcher that used to consume it here is gone; binding a slot to
a feed is ``services/filament_routing``'s job, pinned in
``unit/services/test_filament_routing.py``.
"""

import io
import json
import zipfile

import pytest

from backend.app.services.print_scheduler import PrintScheduler
from backend.app.utils.threemf_tools import extract_nozzle_mapping_from_3mf


class TestSchedulerAmsMappingHelpers:
    """Test the colour helper PrintScheduler still uses."""

    @pytest.fixture
    def scheduler(self):
        return PrintScheduler()

    def test_normalize_color_with_hash(self, scheduler):
        """Color with hash should return #RRGGBB format."""
        result = scheduler._normalize_color("#FF5500")
        assert result == "#FF5500"

    def test_normalize_color_without_hash(self, scheduler):
        """Color without hash should add hash prefix."""
        result = scheduler._normalize_color("FF5500")
        assert result == "#FF5500"

    def test_normalize_color_with_alpha(self, scheduler):
        """Color with alpha channel should strip it."""
        result = scheduler._normalize_color("FF5500AA")
        assert result == "#FF5500"

    def test_normalize_color_none(self, scheduler):
        """None color should return default gray."""
        result = scheduler._normalize_color(None)
        assert result == "#808080"

    def test_normalize_color_empty(self, scheduler):
        """Empty color should return default gray."""
        result = scheduler._normalize_color("")
        assert result == "#808080"


class TestBuildLoadedFilaments:
    """Test the _build_loaded_filaments method."""

    @pytest.fixture
    def scheduler(self):
        return PrintScheduler()

    def test_build_loaded_filaments_empty_status(self, scheduler):
        """Empty status should return empty list."""

        class MockStatus:
            raw_data = {}

        result = scheduler._build_loaded_filaments(MockStatus())
        assert result == []

    def test_build_loaded_filaments_with_ams(self, scheduler):
        """Should extract filaments from AMS units."""

        class MockStatus:
            raw_data = {
                "ams": [
                    {
                        "id": 0,
                        "tray": [
                            {"id": 0, "tray_type": "PLA", "tray_color": "FF0000"},
                            {"id": 1, "tray_type": "PETG", "tray_color": "00FF00"},
                        ],
                    }
                ]
            }

        result = scheduler._build_loaded_filaments(MockStatus())
        assert len(result) == 2

        # First filament
        assert result[0]["type"] == "PLA"
        assert result[0]["color"] == "#FF0000"
        assert result[0]["ams_id"] == 0
        assert result[0]["tray_id"] == 0
        assert result[0]["global_tray_id"] == 0  # 0 * 4 + 0

        # Second filament
        assert result[1]["type"] == "PETG"
        assert result[1]["global_tray_id"] == 1  # 0 * 4 + 1

    def test_build_loaded_filaments_with_ht_ams(self, scheduler):
        """AMS-HT (single tray) should be marked as is_ht."""

        class MockStatus:
            raw_data = {
                "ams": [
                    {
                        "id": 128,
                        "tray": [{"id": 0, "tray_type": "PLA-CF", "tray_color": "000000"}],
                    }
                ]
            }

        result = scheduler._build_loaded_filaments(MockStatus())
        assert len(result) == 1
        assert result[0]["is_ht"] is True
        assert result[0]["global_tray_id"] == 128  # AMS-HT uses ams_id directly

    def test_build_loaded_filaments_with_external(self, scheduler):
        """Should include external spool."""

        class MockStatus:
            raw_data = {"vt_tray": [{"tray_type": "TPU", "tray_color": "0000FF"}]}

        result = scheduler._build_loaded_filaments(MockStatus())
        assert len(result) == 1
        assert result[0]["type"] == "TPU"
        assert result[0]["is_external"] is True
        assert result[0]["global_tray_id"] == 254

    def test_build_loaded_filaments_skips_empty_trays(self, scheduler):
        """Trays without tray_type should be skipped."""

        class MockStatus:
            raw_data = {
                "ams": [
                    {
                        "id": 0,
                        "tray": [
                            {"id": 0, "tray_type": "PLA", "tray_color": "FF0000"},
                            {"id": 1, "tray_type": "", "tray_color": ""},  # Empty
                            {"id": 2},  # No tray_type key
                        ],
                    }
                ]
            }

        result = scheduler._build_loaded_filaments(MockStatus())
        assert len(result) == 1
        assert result[0]["type"] == "PLA"


class TestBuildLoadedFilamentsTrayInfoIdx:
    """Test tray_info_idx extraction in _build_loaded_filaments."""

    @pytest.fixture
    def scheduler(self):
        return PrintScheduler()

    def test_build_loaded_filaments_includes_tray_info_idx(self, scheduler):
        """Should extract tray_info_idx from AMS trays."""

        class MockStatus:
            raw_data = {
                "ams": [
                    {
                        "id": 0,
                        "tray": [
                            {"id": 0, "tray_type": "PLA", "tray_color": "000000", "tray_info_idx": "GFA00"},
                            {"id": 1, "tray_type": "PLA", "tray_color": "000000", "tray_info_idx": "GFA01"},
                        ],
                    }
                ]
            }

        result = scheduler._build_loaded_filaments(MockStatus())
        assert len(result) == 2
        assert result[0]["tray_info_idx"] == "GFA00"
        assert result[1]["tray_info_idx"] == "GFA01"

    def test_build_loaded_filaments_empty_tray_info_idx(self, scheduler):
        """Missing tray_info_idx should default to empty string."""

        class MockStatus:
            raw_data = {
                "ams": [
                    {
                        "id": 0,
                        "tray": [
                            {"id": 0, "tray_type": "PLA", "tray_color": "FF0000"},  # No tray_info_idx
                        ],
                    }
                ]
            }

        result = scheduler._build_loaded_filaments(MockStatus())
        assert len(result) == 1
        assert result[0]["tray_info_idx"] == ""

    def test_build_loaded_filaments_external_spool_tray_info_idx(self, scheduler):
        """Should extract tray_info_idx from external spool."""

        class MockStatus:
            raw_data = {"vt_tray": [{"tray_type": "TPU", "tray_color": "0000FF", "tray_info_idx": "P4d64437"}]}

        result = scheduler._build_loaded_filaments(MockStatus())
        assert len(result) == 1
        assert result[0]["tray_info_idx"] == "P4d64437"
        assert result[0]["is_external"] is True


def _make_3mf_zip(
    project_settings: dict | None = None,
    slice_info_xml: str | None = None,
) -> zipfile.ZipFile:
    """Create an in-memory ZipFile mimicking a 3MF with project_settings.config."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if project_settings is not None:
            zf.writestr("Metadata/project_settings.config", json.dumps(project_settings))
        if slice_info_xml is not None:
            zf.writestr("Metadata/slice_info.config", slice_info_xml)
    buf.seek(0)
    return zipfile.ZipFile(buf, "r")


class TestExtractNozzleMappingFrom3mf:
    """Test the extract_nozzle_mapping_from_3mf utility."""

    def test_group_id_priority_over_filament_nozzle_map(self):
        """group_id from slice_info should override filament_nozzle_map from project_settings.

        Real-world scenario: "Auto For Flush" mode sets filament_nozzle_map all to 0
        (user preference) but the actual assignment in slice_info has different group_ids.
        """
        # filament_nozzle_map says all on slicer ext 0 → MQTT ext 1 (LEFT)
        # But slice_info group_id says slot 6 → group 0 (LEFT), slot 12 → group 1 (RIGHT)
        slice_info = """<?xml version="1.0" encoding="UTF-8"?>
        <config>
          <plate>
            <filament id="6" type="PLA" color="#56B7E6" used_g="1.84" group_id="0"/>
            <filament id="12" type="PLA" color="#B39B84" used_g="1.76" group_id="1"/>
          </plate>
        </config>"""
        zf = _make_3mf_zip(
            {
                "filament_nozzle_map": ["0"] * 12,
                "physical_extruder_map": ["1", "0"],
            },
            slice_info_xml=slice_info,
        )
        result = extract_nozzle_mapping_from_3mf(zf)
        # group_id 0 → physical_extruder_map[0] = 1 (LEFT)
        # group_id 1 → physical_extruder_map[1] = 0 (RIGHT)
        assert result == {6: 1, 12: 0}
        zf.close()

    def test_fallback_to_filament_nozzle_map_without_group_id(self):
        """Should fall back to filament_nozzle_map when slice_info has no group_id."""
        slice_info = """<?xml version="1.0" encoding="UTF-8"?>
        <config>
          <plate>
            <filament id="1" type="PLA" color="#FF0000" used_g="5.0"/>
          </plate>
        </config>"""
        zf = _make_3mf_zip(
            {
                "filament_nozzle_map": ["0", "1", "0"],
                "physical_extruder_map": ["0", "1"],
            },
            slice_info_xml=slice_info,
        )
        result = extract_nozzle_mapping_from_3mf(zf)
        assert result == {1: 0, 2: 1, 3: 0}
        zf.close()

    def test_fallback_to_filament_nozzle_map_without_slice_info(self):
        """Should fall back to filament_nozzle_map when no slice_info.config exists."""
        zf = _make_3mf_zip(
            {
                "filament_nozzle_map": ["0", "1", "0"],
                "physical_extruder_map": ["0", "1"],
            }
        )
        result = extract_nozzle_mapping_from_3mf(zf)
        assert result == {1: 0, 2: 1, 3: 0}
        zf.close()

    def test_single_nozzle_returns_none(self):
        """Single physical_extruder_map entry should return None (single-nozzle)."""
        zf = _make_3mf_zip(
            {
                "filament_nozzle_map": ["0", "0", "0"],
                "physical_extruder_map": ["0"],
            }
        )
        result = extract_nozzle_mapping_from_3mf(zf)
        assert result is None
        zf.close()

    def test_missing_project_settings_returns_none(self):
        """Missing project_settings.config should return None."""
        zf = _make_3mf_zip(None)
        result = extract_nozzle_mapping_from_3mf(zf)
        assert result is None
        zf.close()

    def test_missing_fields_returns_none(self):
        """Missing physical_extruder_map should return None."""
        zf = _make_3mf_zip({"some_other_key": "value"})
        result = extract_nozzle_mapping_from_3mf(zf)
        assert result is None
        zf.close()

    def test_physical_extruder_map_remapping(self):
        """Should apply physical_extruder_map to remap slicer extruder to MQTT extruder."""
        # Slicer ext 0 -> MQTT ext 1, slicer ext 1 -> MQTT ext 0
        zf = _make_3mf_zip(
            {
                "filament_nozzle_map": ["0", "1"],
                "physical_extruder_map": ["1", "0"],
            }
        )
        result = extract_nozzle_mapping_from_3mf(zf)
        assert result == {1: 1, 2: 0}
        zf.close()


class TestNozzleAwareMapping:
    """Test the per-tray extruder id ``_build_loaded_filaments`` reports."""

    @pytest.fixture
    def scheduler(self):
        return PrintScheduler()

    def test_extruder_id_in_loaded_filaments(self, scheduler):
        """_build_loaded_filaments should include extruder_id from ams_extruder_map."""

        class MockStatus:
            raw_data = {
                "ams": [
                    {"id": 0, "tray": [{"id": 0, "tray_type": "PLA", "tray_color": "FF0000"}]},
                    {"id": 1, "tray": [{"id": 0, "tray_type": "PLA", "tray_color": "00FF00"}]},
                ],
                "ams_extruder_map": {"0": 0, "1": 1},
            }

        result = scheduler._build_loaded_filaments(MockStatus())
        assert len(result) == 2
        assert result[0]["extruder_id"] == 0
        assert result[1]["extruder_id"] == 1

    def test_extruder_id_none_without_map(self, scheduler):
        """extruder_id should be None when ams_extruder_map is absent."""

        class MockStatus:
            raw_data = {
                "ams": [
                    {"id": 0, "tray": [{"id": 0, "tray_type": "PLA", "tray_color": "FF0000"}]},
                ]
            }

        result = scheduler._build_loaded_filaments(MockStatus())
        assert len(result) == 1
        assert result[0]["extruder_id"] is None

    def test_external_spool_extruder_id(self, scheduler):
        """External spool 254 (Ext-L) should have extruder_id=1 (LEFT) when ams_extruder_map exists."""

        class MockStatus:
            raw_data = {
                "vt_tray": [{"tray_type": "TPU", "tray_color": "0000FF"}],
                "ams_extruder_map": {"0": 0},
            }

        result = scheduler._build_loaded_filaments(MockStatus())
        assert len(result) == 1
        # Default vt_tray id=254 → Ext-L → LEFT nozzle (extruder 1)
        assert result[0]["extruder_id"] == 1
        assert result[0]["is_external"] is True

    def test_external_spool_no_extruder_map(self, scheduler):
        """External spool extruder_id should be None without ams_extruder_map."""

        class MockStatus:
            raw_data = {"vt_tray": [{"tray_type": "TPU", "tray_color": "0000FF"}]}

        result = scheduler._build_loaded_filaments(MockStatus())
        assert len(result) == 1
        assert result[0]["extruder_id"] is None


# ============================================================================
# MODEL-SPECIFIC TESTS: Real data from actual printers
# ============================================================================


def _h2d_raw_data():
    """H2D real data fixture (from live API response 2026-02-18).

    Configuration:
        LEFT nozzle (extruder 1): AMS 0 (4-slot), AMS 2 (4-slot)
        RIGHT nozzle (extruder 0): AMS 1 (4-slot), AMS-HT 128 (1-slot, empty)
        External: 254 (Ext-L, LEFT), 255 (Ext-R, RIGHT, empty)

    ams_extruder_map: {"0": 1, "1": 0, "2": 1, "128": 0}
    """
    return {
        "ams": [
            {
                "id": 0,
                "tray": [
                    {"id": 0, "tray_type": "PETG", "tray_color": "FFFFFFFF", "tray_info_idx": "GFG02"},
                    {"id": 1, "tray_type": "PLA", "tray_color": "C8C8C8FF", "tray_info_idx": "GFA06"},
                    {"id": 2, "tray_type": "PETG", "tray_color": "875718FF", "tray_info_idx": "GFG02"},
                    {"id": 3, "tray_type": "PLA", "tray_color": "000000FF", "tray_info_idx": "GFA00"},
                ],
            },
            {
                "id": 1,
                "tray": [
                    {"id": 0, "tray_type": "PLA", "tray_color": "FFFFFFFF", "tray_info_idx": "GFA00"},
                    {"id": 1, "tray_type": "PETG", "tray_color": "000000FF", "tray_info_idx": "GFG02"},
                    {"id": 2, "tray_type": "PLA", "tray_color": "5F6367FF", "tray_info_idx": "GFA06"},
                    {"id": 3, "tray_type": "PLA", "tray_color": "B39B84FF", "tray_info_idx": "GFA02"},
                ],
            },
            {
                "id": 128,
                "tray": [{"id": 0}],  # AMS-HT, empty
            },
            {
                "id": 2,
                "tray": [
                    {"id": 0, "tray_type": "PLA-S", "tray_color": "FFFFFFFF", "tray_info_idx": "P8aa1726"},
                    {"id": 1, "tray_type": "PLA", "tray_color": "56B7E6FF", "tray_info_idx": "PFUS9924"},
                    {"id": 2, "tray_type": "PETG", "tray_color": "6EE53CFF", "tray_info_idx": "GFG02"},
                    {"id": 3, "tray_type": "PLA", "tray_color": "FF0000FF", "tray_info_idx": "PFUS9ac9"},
                ],
            },
        ],
        "vt_tray": [
            {"id": 254, "tray_type": "PLA", "tray_color": "000000FF", "tray_info_idx": "P4d64437"},
            {"id": 255, "tray_type": "", "tray_color": "00000000"},  # empty
        ],
        "ams_extruder_map": {"0": 1, "1": 0, "2": 1, "128": 0},
    }


def _x1c_raw_data():
    """X1C real data fixture (from live API response 2026-02-18).

    Configuration:
        Single nozzle (extruder 0): AMS 0 (4-slot, all empty), AMS 1 (4-slot, 3 loaded)
        External: 254 (single, empty)

    ams_extruder_map: {"0": 0, "1": 0}  ← NOT empty, all on extruder 0
    """
    return {
        "ams": [
            {
                "id": 0,
                "tray": [
                    {"id": 0},  # empty
                    {"id": 1},  # empty
                    {"id": 2},  # empty
                    {"id": 3},  # empty
                ],
            },
            {
                "id": 1,
                "tray": [
                    {"id": 0},  # empty
                    {"id": 1, "tray_type": "PLA", "tray_color": "EBCFA6FF", "tray_info_idx": "PFUS22b2"},
                    {"id": 2, "tray_type": "PLA", "tray_color": "FCECD6FF", "tray_info_idx": "P4d64437"},
                    {"id": 3, "tray_type": "PLA", "tray_color": "0066FFFF", "tray_info_idx": "P4d64437"},
                ],
            },
        ],
        "vt_tray": [
            {"id": 254, "tray_type": "", "tray_color": "00000000"},  # empty
        ],
        "ams_extruder_map": {"0": 0, "1": 0},
    }


class TestH2DModel:
    """H2D-specific tests with real printer data (dual nozzle, AMS-HT)."""

    @pytest.fixture
    def scheduler(self):
        return PrintScheduler()

    def test_build_loaded_filaments_h2d(self, scheduler):
        """H2D: correct extruder_id, global_tray_id, AMS-HT handling."""

        class MockStatus:
            raw_data = _h2d_raw_data()

        result = scheduler._build_loaded_filaments(MockStatus())

        # Should have 13 loaded filaments (4 + 4 + 0 + 4 + 1 external)
        assert len(result) == 13

        # AMS 0 trays → extruder 1 (LEFT)
        ams0 = [f for f in result if f["ams_id"] == 0]
        assert len(ams0) == 4
        assert all(f["extruder_id"] == 1 for f in ams0)
        assert [f["global_tray_id"] for f in ams0] == [0, 1, 2, 3]

        # AMS 1 trays → extruder 0 (RIGHT)
        ams1 = [f for f in result if f["ams_id"] == 1]
        assert len(ams1) == 4
        assert all(f["extruder_id"] == 0 for f in ams1)
        assert [f["global_tray_id"] for f in ams1] == [4, 5, 6, 7]

        # AMS-HT 128 → empty, should not appear
        ams_ht = [f for f in result if f["ams_id"] == 128]
        assert len(ams_ht) == 0

        # AMS 2 trays → extruder 1 (LEFT)
        ams2 = [f for f in result if f["ams_id"] == 2]
        assert len(ams2) == 4
        assert all(f["extruder_id"] == 1 for f in ams2)
        assert [f["global_tray_id"] for f in ams2] == [8, 9, 10, 11]

    def test_external_spool_extruder_h2d(self, scheduler):
        """H2D: Ext-L (254) = LEFT (extruder 1), Ext-R (255) = RIGHT (extruder 0)."""

        class MockStatus:
            raw_data = _h2d_raw_data()

        result = scheduler._build_loaded_filaments(MockStatus())
        ext = [f for f in result if f["is_external"]]
        assert len(ext) == 1  # Only 254 has filament
        assert ext[0]["global_tray_id"] == 254
        # Ext-L (254) should be LEFT nozzle (extruder 1)
        assert ext[0]["extruder_id"] == 1


class TestX1CModel:
    """X1C-specific tests with real printer data (single nozzle, 2x regular AMS)."""

    @pytest.fixture
    def scheduler(self):
        return PrintScheduler()

    def test_build_loaded_filaments_x1c(self, scheduler):
        """X1C: all filaments on extruder 0, correct global_tray_id."""

        class MockStatus:
            raw_data = _x1c_raw_data()

        result = scheduler._build_loaded_filaments(MockStatus())

        # Only 3 loaded (AMS 1 trays 1-3)
        assert len(result) == 3
        # All on extruder 0
        assert all(f["extruder_id"] == 0 for f in result)
        # Correct global tray IDs
        assert [f["global_tray_id"] for f in result] == [5, 6, 7]
