"""Unit tests for 3MF parsing utilities (threemf_tools.py).

Tests G-code parsing, filament length-to-weight conversion,
and cumulative layer usage lookup.
"""

import io
import json
import math
import zipfile
from contextlib import contextmanager

from backend.app.utils.threemf_tools import (
    expand_to_project_slots,
    extract_bed_type_from_3mf,
    extract_embedded_presets_from_3mf,
    extract_filament_usage_from_3mf,
    extract_support_filament_slots_from_3mf,
    get_cumulative_usage_at_layer,
    mm_to_grams,
    parse_gcode_layer_filament_usage,
)


@contextmanager
def _make_3mf_with(files: dict[str, str]):
    """Yield an open in-memory 3MF (ZIP) containing the given path → text map."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    buffer.seek(0)
    with zipfile.ZipFile(buffer, "r") as zf:
        yield zf


def create_mock_3mf(slice_info_content: str) -> io.BytesIO:
    """Create a mock 3MF file (ZIP) with slice_info.config content."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("Metadata/slice_info.config", slice_info_content)
    buffer.seek(0)
    return buffer


class TestParseGcodeLayerFilamentUsage:
    """Tests for parse_gcode_layer_filament_usage()."""

    def test_single_filament_single_layer(self):
        """Single filament extruding on one layer."""
        gcode = """
M620 S0
G1 X10 Y10 E5.0
G1 X20 Y20 E3.0
"""
        result = parse_gcode_layer_filament_usage(gcode)
        assert result == {0: {0: 8.0}}

    def test_multi_layer_single_filament(self):
        """Single filament across multiple layers."""
        gcode = """
M620 S0
G1 X10 Y10 E10.0
M73 L1
G1 X20 Y20 E5.0
M73 L2
G1 X30 Y30 E7.0
"""
        result = parse_gcode_layer_filament_usage(gcode)
        assert result[0] == {0: 10.0}
        assert result[1] == {0: 15.0}
        assert result[2] == {0: 22.0}

    def test_multi_material(self):
        """Multiple filaments switching via M620."""
        gcode = """
M620 S0
G1 E10.0
M73 L1
M620 S1
G1 E5.0
M620 S0
G1 E3.0
M73 L2
G1 E2.0
"""
        result = parse_gcode_layer_filament_usage(gcode)
        # Layer 0: filament 0 = 10mm
        assert result[0] == {0: 10.0}
        # Layer 1: filament 0 = 13mm (10+3), filament 1 = 5mm
        assert result[1] == {0: 13.0, 1: 5.0}
        # Layer 2: filament 0 = 15mm (13+2)
        assert result[2] == {0: 15.0, 1: 5.0}

    def test_retractions_ignored(self):
        """Negative E values (retractions) should be ignored."""
        gcode = """
M620 S0
G1 E10.0
G1 E-2.0
G1 E5.0
"""
        result = parse_gcode_layer_filament_usage(gcode)
        assert result == {0: {0: 15.0}}

    def test_m620_s255_unloads(self):
        """M620 S255 means unload - extrusion after should be ignored."""
        gcode = """
M620 S0
G1 E10.0
M620 S255
G1 E5.0
"""
        result = parse_gcode_layer_filament_usage(gcode)
        assert result == {0: {0: 10.0}}

    def test_m620_with_suffix(self):
        """M620 S0A format (filament ID with suffix letter)."""
        gcode = """
M620 S0A
G1 E10.0
M620 S1A
G1 E5.0
"""
        result = parse_gcode_layer_filament_usage(gcode)
        assert result == {0: {0: 10.0, 1: 5.0}}

    def test_comments_ignored(self):
        """Comment lines and inline comments are ignored."""
        gcode = """
; This is a comment
M620 S0
G1 X10 E5.0 ; inline comment with E value
G1 E3.0
"""
        result = parse_gcode_layer_filament_usage(gcode)
        assert result == {0: {0: 8.0}}

    def test_empty_gcode(self):
        """Empty G-code returns empty dict."""
        assert parse_gcode_layer_filament_usage("") == {}
        assert parse_gcode_layer_filament_usage("\n\n\n") == {}

    def test_no_extrusion(self):
        """G-code with moves but no extrusion."""
        gcode = """
G1 X10 Y10
G1 X20 Y20
"""
        assert parse_gcode_layer_filament_usage(gcode) == {}

    def test_no_active_filament_extrusion_ignored(self):
        """Extrusion before any M620 is ignored (no active filament)."""
        gcode = """
G1 E10.0
M620 S0
G1 E5.0
"""
        result = parse_gcode_layer_filament_usage(gcode)
        assert result == {0: {0: 5.0}}

    def test_g0_g2_g3_extrusion(self):
        """G0, G2, G3 with E parameter are also tracked."""
        gcode = """
M620 S0
G0 E1.0
G1 E2.0
G2 E3.0
G3 E4.0
"""
        result = parse_gcode_layer_filament_usage(gcode)
        assert result == {0: {0: 10.0}}

    def test_cumulative_across_layers(self):
        """Values are cumulative, not per-layer."""
        gcode = """
M620 S0
G1 E100.0
M73 L1
G1 E100.0
M73 L2
G1 E100.0
"""
        result = parse_gcode_layer_filament_usage(gcode)
        assert result[0] == {0: 100.0}
        assert result[1] == {0: 200.0}
        assert result[2] == {0: 300.0}


class TestMmToGrams:
    """Tests for mm_to_grams()."""

    def test_default_pla_175(self):
        """Default PLA 1.75mm conversion."""
        # 1000mm of 1.75mm PLA at 1.24 g/cm³
        # Volume = π × (0.0875cm)² × 100cm = 2.405cm³
        # Weight = 2.405 × 1.24 = 2.982g
        result = mm_to_grams(1000.0)
        expected = math.pi * (0.0875**2) * 100 * 1.24
        assert abs(result - expected) < 0.001

    def test_zero_length(self):
        """Zero length returns zero weight."""
        assert mm_to_grams(0.0) == 0.0

    def test_custom_diameter(self):
        """Custom diameter (2.85mm) changes result."""
        result_175 = mm_to_grams(1000.0, diameter_mm=1.75)
        result_285 = mm_to_grams(1000.0, diameter_mm=2.85)
        # 2.85mm filament has more volume per mm
        assert result_285 > result_175
        ratio = (2.85 / 1.75) ** 2  # Volume scales with diameter²
        assert abs(result_285 / result_175 - ratio) < 0.001

    def test_custom_density(self):
        """Different density (ABS vs PLA)."""
        pla = mm_to_grams(1000.0, density_g_cm3=1.24)
        abs_ = mm_to_grams(1000.0, density_g_cm3=1.04)
        assert pla > abs_
        assert abs(pla / abs_ - 1.24 / 1.04) < 0.001

    def test_known_value(self):
        """Verify against a known calculation.

        1m (1000mm) of 1.75mm PLA at 1.24 g/cm³:
        r = 0.0875 cm, L = 100 cm
        V = π × 0.0875² × 100 = 2.4053 cm³
        m = 2.4053 × 1.24 = 2.9826 g
        """
        result = mm_to_grams(1000.0, 1.75, 1.24)
        assert abs(result - 2.9826) < 0.01


class TestGetCumulativeUsageAtLayer:
    """Tests for get_cumulative_usage_at_layer()."""

    def test_exact_layer_match(self):
        """Target layer exists exactly in the data."""
        data = {0: {0: 100.0}, 5: {0: 500.0}, 10: {0: 1000.0}}
        assert get_cumulative_usage_at_layer(data, 5) == {0: 500.0}

    def test_between_layers(self):
        """Target is between recorded layers - uses the closest lower one."""
        data = {0: {0: 100.0}, 5: {0: 500.0}, 10: {0: 1000.0}}
        # Layer 7 is between 5 and 10, should return layer 5's data
        assert get_cumulative_usage_at_layer(data, 7) == {0: 500.0}

    def test_beyond_last_layer(self):
        """Target is beyond the last recorded layer."""
        data = {0: {0: 100.0}, 5: {0: 500.0}}
        assert get_cumulative_usage_at_layer(data, 100) == {0: 500.0}

    def test_before_first_layer(self):
        """Target is before any recorded data."""
        data = {5: {0: 500.0}, 10: {0: 1000.0}}
        assert get_cumulative_usage_at_layer(data, 3) == {}

    def test_empty_data(self):
        """Empty layer_usage returns empty dict."""
        assert get_cumulative_usage_at_layer({}, 5) == {}

    def test_none_data(self):
        """None layer_usage returns empty dict."""
        assert get_cumulative_usage_at_layer(None, 5) == {}

    def test_multi_filament(self):
        """Multi-filament data at target layer."""
        data = {
            0: {0: 50.0},
            5: {0: 200.0, 1: 100.0},
            10: {0: 400.0, 1: 250.0, 2: 50.0},
        }
        result = get_cumulative_usage_at_layer(data, 8)
        assert result == {0: 200.0, 1: 100.0}

    def test_layer_zero(self):
        """Target layer 0."""
        data = {0: {0: 10.0}, 1: {0: 20.0}}
        assert get_cumulative_usage_at_layer(data, 0) == {0: 10.0}


class TestExtractFilamentUsageFrom3mf:
    """Tests for extract_filament_usage_from_3mf function."""

    def test_extract_single_filament(self, tmp_path):
        """Test extracting a single filament."""
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
        <config>
            <filament id="1" used_g="50.5" type="PLA" color="#FF0000"/>
        </config>
        """
        mock_3mf = create_mock_3mf(xml_content)
        file_path = tmp_path / "test.3mf"
        file_path.write_bytes(mock_3mf.read())

        result = extract_filament_usage_from_3mf(file_path)

        assert len(result) == 1
        assert result[0]["slot_id"] == 1
        assert result[0]["used_g"] == 50.5
        assert result[0]["type"] == "PLA"
        assert result[0]["color"] == "#FF0000"

    def test_extract_multiple_filaments(self, tmp_path):
        """Test extracting multiple filaments."""
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
        <config>
            <filament id="1" used_g="50.5" type="PLA" color="#FF0000"/>
            <filament id="2" used_g="30.2" type="PETG" color="#00FF00"/>
            <filament id="3" used_g="10.0" type="ABS" color="#0000FF"/>
        </config>
        """
        mock_3mf = create_mock_3mf(xml_content)
        file_path = tmp_path / "test.3mf"
        file_path.write_bytes(mock_3mf.read())

        result = extract_filament_usage_from_3mf(file_path)

        assert len(result) == 3
        assert result[0]["slot_id"] == 1
        assert result[1]["slot_id"] == 2
        assert result[2]["slot_id"] == 3

    def test_extract_filament_with_plate_id(self, tmp_path):
        """Test extracting filament for a specific plate."""
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
        <config>
            <plate>
                <metadata key="index" value="1"/>
                <filament id="1" used_g="25.0" type="PLA" color="#FF0000"/>
            </plate>
            <plate>
                <metadata key="index" value="2"/>
                <filament id="1" used_g="75.0" type="PETG" color="#00FF00"/>
            </plate>
        </config>
        """
        mock_3mf = create_mock_3mf(xml_content)
        file_path = tmp_path / "test.3mf"
        file_path.write_bytes(mock_3mf.read())

        result = extract_filament_usage_from_3mf(file_path, plate_id=2)

        assert len(result) == 1
        assert result[0]["used_g"] == 75.0
        assert result[0]["type"] == "PETG"

    def test_missing_slice_info_returns_empty(self, tmp_path):
        """Test that missing slice_info.config returns empty list."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr("other_file.txt", "content")
        buffer.seek(0)

        file_path = tmp_path / "test.3mf"
        file_path.write_bytes(buffer.read())

        result = extract_filament_usage_from_3mf(file_path)

        assert result == []

    def test_invalid_file_returns_empty(self, tmp_path):
        """Test that invalid file returns empty list."""
        file_path = tmp_path / "invalid.3mf"
        file_path.write_text("not a zip file")

        result = extract_filament_usage_from_3mf(file_path)

        assert result == []

    def test_nonexistent_file_returns_empty(self, tmp_path):
        """Test that nonexistent file returns empty list."""
        file_path = tmp_path / "nonexistent.3mf"

        result = extract_filament_usage_from_3mf(file_path)

        assert result == []

    def test_filament_without_id_is_skipped(self, tmp_path):
        """Test that filament without id is skipped."""
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
        <config>
            <filament used_g="50.5" type="PLA" color="#FF0000"/>
            <filament id="2" used_g="30.0" type="PETG" color="#00FF00"/>
        </config>
        """
        mock_3mf = create_mock_3mf(xml_content)
        file_path = tmp_path / "test.3mf"
        file_path.write_bytes(mock_3mf.read())

        result = extract_filament_usage_from_3mf(file_path)

        assert len(result) == 1
        assert result[0]["slot_id"] == 2

    def test_invalid_used_g_is_skipped(self, tmp_path):
        """Test that filament with invalid used_g is skipped."""
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
        <config>
            <filament id="1" used_g="invalid" type="PLA" color="#FF0000"/>
            <filament id="2" used_g="30.0" type="PETG" color="#00FF00"/>
        </config>
        """
        mock_3mf = create_mock_3mf(xml_content)
        file_path = tmp_path / "test.3mf"
        file_path.write_bytes(mock_3mf.read())

        result = extract_filament_usage_from_3mf(file_path)

        assert len(result) == 1
        assert result[0]["slot_id"] == 2

    def test_missing_optional_fields(self, tmp_path):
        """Test that missing type and color default to empty string."""
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
        <config>
            <filament id="1" used_g="50.5"/>
        </config>
        """
        mock_3mf = create_mock_3mf(xml_content)
        file_path = tmp_path / "test.3mf"
        file_path.write_bytes(mock_3mf.read())

        result = extract_filament_usage_from_3mf(file_path)

        assert len(result) == 1
        assert result[0]["type"] == ""
        assert result[0]["color"] == ""


class TestExtractEmbeddedPresetsFrom3mf:
    """Printer / process preset names read from project_settings.config so the
    SliceModal can default its dropdowns to the file's own config (#1325)."""

    def test_extracts_printer_and_process(self):
        config = json.dumps(
            {
                "printer_settings_id": "Bambu Lab X1 Carbon 0.4 nozzle",
                "print_settings_id": "0.20mm Standard @BBL X1C",
                "filament_settings_id": ["Bambu PLA Basic @BBL X1C"],
            }
        )
        with _make_3mf_with({"Metadata/project_settings.config": config}) as zf:
            assert extract_embedded_presets_from_3mf(zf) == {
                "printer": "Bambu Lab X1 Carbon 0.4 nozzle",
                "process": "0.20mm Standard @BBL X1C",
            }

    def test_settings_id_as_list_takes_first(self):
        # Some exports write *_settings_id as a per-extruder list.
        config = json.dumps(
            {
                "printer_settings_id": ["Bambu Lab A1 0.4 nozzle"],
                "print_settings_id": ["0.16mm Optimal @BBL A1", "0.20mm @BBL A1"],
            }
        )
        with _make_3mf_with({"Metadata/project_settings.config": config}) as zf:
            result = extract_embedded_presets_from_3mf(zf)
            assert result["printer"] == "Bambu Lab A1 0.4 nozzle"
            assert result["process"] == "0.16mm Optimal @BBL A1"

    def test_missing_config_returns_none_values(self):
        with _make_3mf_with({"3D/3dmodel.model": "<model/>"}) as zf:
            assert extract_embedded_presets_from_3mf(zf) == {
                "printer": None,
                "process": None,
            }

    def test_malformed_json_returns_none_values(self):
        with _make_3mf_with({"Metadata/project_settings.config": "not json"}) as zf:
            assert extract_embedded_presets_from_3mf(zf) == {
                "printer": None,
                "process": None,
            }

    def test_blank_and_absent_keys_yield_none(self):
        config = json.dumps({"printer_settings_id": "  ", "other": "x"})
        with _make_3mf_with({"Metadata/project_settings.config": config}) as zf:
            assert extract_embedded_presets_from_3mf(zf) == {
                "printer": None,
                "process": None,
            }


class TestExtractBedTypeFrom3mf:
    """extract_bed_type_from_3mf reads per-plate ``curr_bed_type`` from
    slice_info.config so the queue / print modal can show the right plate
    even on multi-plate 3MFs where different plates target different beds
    (#1281). ``archive.bed_type`` is one-value-per-archive (the first plate's
    curr_bed_type), so accurate per-plate surfacing has to re-read the 3MF."""

    def test_single_plate_returns_bed_type(self, tmp_path):
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
        <config>
            <plate>
                <metadata key="index" value="1"/>
                <metadata key="curr_bed_type" value="Textured PEI Plate"/>
            </plate>
        </config>
        """
        file_path = tmp_path / "test.3mf"
        file_path.write_bytes(create_mock_3mf(xml_content).read())

        assert extract_bed_type_from_3mf(file_path) == "Textured PEI Plate"

    def test_multi_plate_returns_per_plate_value(self, tmp_path):
        # Reporter's case: a 3MF mixing PEI + Engineering across plates.
        # Looking up by plate_id must return THAT plate's value, not the
        # first plate's value the archive happens to cache.
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
        <config>
            <plate>
                <metadata key="index" value="1"/>
                <metadata key="curr_bed_type" value="Textured PEI Plate"/>
            </plate>
            <plate>
                <metadata key="index" value="2"/>
                <metadata key="curr_bed_type" value="Engineering Plate"/>
            </plate>
            <plate>
                <metadata key="index" value="3"/>
                <metadata key="curr_bed_type" value="Cool Plate"/>
            </plate>
        </config>
        """
        file_path = tmp_path / "test.3mf"
        file_path.write_bytes(create_mock_3mf(xml_content).read())

        assert extract_bed_type_from_3mf(file_path, plate_id=1) == "Textured PEI Plate"
        assert extract_bed_type_from_3mf(file_path, plate_id=2) == "Engineering Plate"
        assert extract_bed_type_from_3mf(file_path, plate_id=3) == "Cool Plate"

    def test_no_plate_id_returns_first_plate(self, tmp_path):
        # The plate_id=None branch matches the archive-level capture
        # convention (first plate wins) so callers that don't care about
        # plate selection see the same value the archive table holds.
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
        <config>
            <plate>
                <metadata key="index" value="1"/>
                <metadata key="curr_bed_type" value="Cool Plate SuperTack"/>
            </plate>
            <plate>
                <metadata key="index" value="2"/>
                <metadata key="curr_bed_type" value="Engineering Plate"/>
            </plate>
        </config>
        """
        file_path = tmp_path / "test.3mf"
        file_path.write_bytes(create_mock_3mf(xml_content).read())

        assert extract_bed_type_from_3mf(file_path) == "Cool Plate SuperTack"

    def test_unknown_plate_id_returns_none(self, tmp_path):
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
        <config>
            <plate>
                <metadata key="index" value="1"/>
                <metadata key="curr_bed_type" value="Textured PEI Plate"/>
            </plate>
        </config>
        """
        file_path = tmp_path / "test.3mf"
        file_path.write_bytes(create_mock_3mf(xml_content).read())

        assert extract_bed_type_from_3mf(file_path, plate_id=99) is None

    def test_plate_without_bed_type_returns_none(self, tmp_path):
        # Older slicers may export a plate without curr_bed_type. The helper
        # must return None rather than falling through to another plate's
        # value (which would silently lie).
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
        <config>
            <plate>
                <metadata key="index" value="1"/>
            </plate>
            <plate>
                <metadata key="index" value="2"/>
                <metadata key="curr_bed_type" value="Engineering Plate"/>
            </plate>
        </config>
        """
        file_path = tmp_path / "test.3mf"
        file_path.write_bytes(create_mock_3mf(xml_content).read())

        assert extract_bed_type_from_3mf(file_path, plate_id=1) is None
        assert extract_bed_type_from_3mf(file_path, plate_id=2) == "Engineering Plate"

    def test_missing_slice_info_returns_none(self, tmp_path):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr("other_file.txt", "content")
        buffer.seek(0)

        file_path = tmp_path / "test.3mf"
        file_path.write_bytes(buffer.read())

        assert extract_bed_type_from_3mf(file_path) is None

    def test_invalid_file_returns_none(self, tmp_path):
        file_path = tmp_path / "invalid.3mf"
        file_path.write_text("not a zip file")

        assert extract_bed_type_from_3mf(file_path) is None

    def test_whitespace_trimmed(self, tmp_path):
        # 3MF values sometimes carry surrounding whitespace from manual
        # template tweaks; the wire shape should be clean.
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
        <config>
            <plate>
                <metadata key="index" value="1"/>
                <metadata key="curr_bed_type" value="  Textured PEI Plate  "/>
            </plate>
        </config>
        """
        file_path = tmp_path / "test.3mf"
        file_path.write_bytes(create_mock_3mf(xml_content).read())

        assert extract_bed_type_from_3mf(file_path) == "Textured PEI Plate"


# ---------------------------------------------------------------------------
# Tests for extract_support_filament_slots_from_3mf — #1881: a plate that uses
# PVA (or any material) exclusively for supports doesn't reference the support
# slot from object geometry, so without this helper substitute_unused_plate_
# filaments overwrites the user's support-material profile with slot 1's.
# ---------------------------------------------------------------------------


class TestExtractSupportFilamentSlotsFrom3mf:
    def test_pla_object_plus_pva_support_returns_support_slot(self):
        # The reporter's exact scenario (#1881): slot 1 = PLA (model),
        # slot 2 = PVA (support). enable_support on. Without this the
        # substitute logic replaces slot 2's PVA profile with PLA and
        # the printed supports come out in PLA.
        cfg = json.dumps(
            {
                "enable_support": "1",
                "support_filament": "2",
                "support_interface_filament": "2",
                "filament_type": ["PLA", "PVA"],
            }
        )
        with _make_3mf_with({"Metadata/project_settings.config": cfg}) as zf:
            assert extract_support_filament_slots_from_3mf(zf) == {2}

    def test_distinct_support_body_and_interface_slots(self):
        cfg = json.dumps(
            {
                "enable_support": "1",
                "support_filament": "2",
                "support_interface_filament": "3",
            }
        )
        with _make_3mf_with({"Metadata/project_settings.config": cfg}) as zf:
            assert extract_support_filament_slots_from_3mf(zf) == {2, 3}

    def test_supports_disabled_returns_empty(self):
        # enable_support off — supports won't be printed even if a slot is
        # configured. Don't force it into the "used" set; substitution
        # should still homogenise the loaded-filament array.
        cfg = json.dumps(
            {
                "enable_support": "0",
                "support_filament": "2",
                "support_interface_filament": "2",
            }
        )
        with _make_3mf_with({"Metadata/project_settings.config": cfg}) as zf:
            assert extract_support_filament_slots_from_3mf(zf) == set()

    def test_slot_zero_treated_as_same_as_model(self):
        # BambuStudio's `0` for support_filament means "same as model" —
        # no dedicated slot to preserve.
        cfg = json.dumps(
            {
                "enable_support": "1",
                "support_filament": "0",
                "support_interface_filament": "0",
            }
        )
        with _make_3mf_with({"Metadata/project_settings.config": cfg}) as zf:
            assert extract_support_filament_slots_from_3mf(zf) == set()

    def test_boolean_enable_support_accepted(self):
        # Some forks / older versions write a real JSON bool instead of "1"/"0".
        cfg = json.dumps({"enable_support": True, "support_filament": "2"})
        with _make_3mf_with({"Metadata/project_settings.config": cfg}) as zf:
            assert extract_support_filament_slots_from_3mf(zf) == {2}

    def test_integer_slot_value_accepted(self):
        cfg = json.dumps({"enable_support": "1", "support_filament": 3})
        with _make_3mf_with({"Metadata/project_settings.config": cfg}) as zf:
            assert extract_support_filament_slots_from_3mf(zf) == {3}

    def test_missing_project_settings_returns_empty(self):
        with _make_3mf_with({"placeholder.txt": "hi"}) as zf:
            assert extract_support_filament_slots_from_3mf(zf) == set()

    def test_malformed_json_returns_empty(self):
        with _make_3mf_with({"Metadata/project_settings.config": "{not json"}) as zf:
            assert extract_support_filament_slots_from_3mf(zf) == set()

    def test_root_is_list_returns_empty(self):
        with _make_3mf_with({"Metadata/project_settings.config": json.dumps([])}) as zf:
            assert extract_support_filament_slots_from_3mf(zf) == set()

    def test_non_numeric_slot_value_skipped(self):
        cfg = json.dumps({"enable_support": "1", "support_filament": "not-a-number"})
        with _make_3mf_with({"Metadata/project_settings.config": cfg}) as zf:
            assert extract_support_filament_slots_from_3mf(zf) == set()


class TestExpandToProjectSlots:
    """Widening the used-only list to every project slot (#2712).

    The slice modal's filament list is **positional** — index 0 is slot 1, all
    the way down to the CLI's ``filament_N.json`` parts. A source that declares
    four filaments but paints with slot 4 alone used to yield a single row, so
    the user's pick bound to slot 1 and slot 4 printed with whatever the source
    had baked in. Wrong material, no warning.
    """

    PROJECT_4 = json.dumps(
        {
            "filament_type": ["PLA", "PLA", "PETG", "PETG"],
            "filament_colour": ["#FF0000", "#00FF00", "#0000FF", "#FFFFFF"],
        }
    )

    def _used(self, *slot_ids: int) -> list[dict]:
        return [
            {
                "slot_id": s,
                "type": "PETG",
                "color": "#FFFFFF",
                "used_grams": 12.5,
                "used_meters": 4.0,
                "tray_info_idx": "GFG99",
                "used_in_plate": True,
            }
            for s in slot_ids
        ]

    def test_the_reported_case_one_used_slot_becomes_four_rows(self):
        with _make_3mf_with({"Metadata/project_settings.config": self.PROJECT_4}) as zf:
            out = expand_to_project_slots(zf, self._used(4))

        assert [f["slot_id"] for f in out] == [1, 2, 3, 4]
        assert [f["used_in_plate"] for f in out] == [False, False, False, True]

    def test_the_used_row_keeps_its_own_figures(self):
        """Widening must not overwrite what slice_info actually measured — the
        usage, the resolved colour and the tray_info_idx are the real answer."""
        with _make_3mf_with({"Metadata/project_settings.config": self.PROJECT_4}) as zf:
            out = expand_to_project_slots(zf, self._used(4))

        slot4 = out[3]
        assert slot4["used_grams"] == 12.5
        assert slot4["tray_info_idx"] == "GFG99"

    def test_filler_rows_carry_the_project_configuration(self):
        with _make_3mf_with({"Metadata/project_settings.config": self.PROJECT_4}) as zf:
            out = expand_to_project_slots(zf, self._used(4))

        assert out[0]["type"] == "PLA"
        assert out[0]["color"] == "#FF0000"
        assert out[0]["used_grams"] == 0

    def test_a_used_slot_the_project_does_not_declare_is_appended(self):
        """A superset was asked for. Dropping the one slot that actually prints
        would be the original bug wearing a different hat."""
        two_slot = json.dumps({"filament_type": ["PLA", "PLA"], "filament_colour": ["#000", "#111"]})
        with _make_3mf_with({"Metadata/project_settings.config": two_slot}) as zf:
            out = expand_to_project_slots(zf, self._used(5))

        assert [f["slot_id"] for f in out] == [1, 2, 5]
        assert out[2]["used_in_plate"] is True

    def test_every_slot_used_stays_every_slot_used(self):
        with _make_3mf_with({"Metadata/project_settings.config": self.PROJECT_4}) as zf:
            out = expand_to_project_slots(zf, self._used(1, 2, 3, 4))

        assert all(f["used_in_plate"] for f in out)
        assert len(out) == 4

    def test_no_project_settings_returns_the_input_untouched(self):
        """A narrower-than-ideal list still prints correctly; an invented one
        might not."""
        used = self._used(4)
        with _make_3mf_with({"placeholder.txt": "hi"}) as zf:
            assert expand_to_project_slots(zf, used) == used

    def test_malformed_project_settings_returns_the_input_untouched(self):
        used = self._used(2)
        with _make_3mf_with({"Metadata/project_settings.config": "{not json"}) as zf:
            assert expand_to_project_slots(zf, used) == used
