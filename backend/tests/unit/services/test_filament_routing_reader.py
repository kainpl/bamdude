"""Print-time evidence must retain plate, usage and nozzle identity."""

import zipfile

import pytest

from backend.app.services.filament_requirements import (
    SourceIdentity,
    extract_filament_requirements,
    read_print_requirements,
)
from backend.app.utils.printer_models import DUAL_NOZZLE_MODELS
from backend.tests.fixtures.filament_routing_cases import DUAL_SETTINGS, mixed_filaments, write_routing_3mf


@pytest.mark.parametrize("model", sorted(DUAL_NOZZLE_MODELS))
def test_legacy_wrapper_keeps_nozzles_and_tiny_used_channels(tmp_path, model):
    source = write_routing_3mf(
        tmp_path / "mixed.3mf",
        {1: mixed_filaments(), 4: mixed_filaments(reverse=True)},
        model=model,
        settings=DUAL_SETTINGS,
    )
    slots = extract_filament_requirements(source, plate_id=4)
    assert [(s["slot_id"], s.get("nozzle_id")) for s in slots] == [(1, 0), (2, 1)]
    assert slots[1]["used_grams"] == 0.0001


SINGLE = [{"id": 3, "type": "PLA", "color": "#FF0000", "tray_info_idx": "GFA01", "used_g": "12.25"}]


@pytest.mark.parametrize("index", [1, 2, 3, 4, 15])
@pytest.mark.parametrize("requested", [None, 0])
def test_whole_file_resolves_actual_single_plate(tmp_path, index, requested):
    source = write_routing_3mf(tmp_path / "single.3mf", {index: SINGLE})
    result = read_print_requirements(str(source), requested)
    assert result.status == "ok"
    assert result.reason is None
    assert result.resolved_plate_id == index
    assert result.gcode_member == f"Metadata/plate_{index}.gcode"
    assert result.model == "P1P"
    assert result.source_identity == SourceIdentity.of(source)
    assert result.used_filaments == (
        {
            "slot_id": 3,
            "type": "PLA",
            "color": "#FF0000",
            "tray_info_idx": "GFA01",
            "used_grams": 12.25,
            "nozzle_id": None,
        },
    )


@pytest.mark.parametrize(
    ("requested", "hint", "expected", "reason"),
    [
        (None, None, None, "plate_selection_required"),
        (None, 4, 4, None),
        (0, 4, None, "plate_selection_required"),
        (1, 4, 1, None),
        (2, 4, None, "plate_not_found"),
        (1, -1, 1, None),
        (-1, 4, None, "invalid_plate_id"),
        (True, None, None, "invalid_plate_id"),
        ("1", None, None, "invalid_plate_id"),
    ],
)
def test_explicit_plate_overrides_hint_and_never_falls_back(tmp_path, requested, hint, expected, reason):
    source = write_routing_3mf(tmp_path / "multi.3mf", {4: SINGLE, 1: SINGLE})
    result = read_print_requirements(source, requested, archive_plate_id=hint)
    assert result.reason == reason
    assert result.resolved_plate_id == expected
    assert result.status == ("unavailable" if reason else "ok")
    if reason:
        assert result.used_filaments == ()


def test_only_printable_plate_can_be_selected_implicitly(tmp_path):
    source = write_routing_3mf(tmp_path / "multi.3mf", {1: SINGLE, 15: SINGLE}, gcode_plates=[15])
    assert read_print_requirements(source).resolved_plate_id == 15
    assert read_print_requirements(source, 1).reason == "plate_gcode_missing"
    assert read_print_requirements(source, archive_plate_id=1).reason == "plate_gcode_missing"


@pytest.mark.parametrize("usage", [None, "", "invalid", "NaN", "inf", "-inf", "-1"])
def test_unknown_or_invalid_usage_refuses_entire_plate(tmp_path, usage):
    source = write_routing_3mf(tmp_path / "unknown.3mf", {1: [*SINGLE, {"id": 2, "type": "PETG", "used_g": usage}]})
    result = read_print_requirements(source)
    assert result.status == "unavailable"
    assert result.reason == "filament_usage_unavailable"
    assert result.used_filaments == ()  # The known slot cannot hide the unknown one.


@pytest.mark.parametrize("filaments", [[], [{"id": 1, "type": "PLA", "used_g": 0}], [*SINGLE, *SINGLE]])
def test_no_used_slots_and_duplicate_slot_ids_are_not_success(tmp_path, filaments):
    source = write_routing_3mf(tmp_path / "unknown.3mf", {1: filaments})
    assert read_print_requirements(source).reason == "filament_usage_unavailable"


def test_zero_tiny_usage_and_unknown_colour_stay_distinct(tmp_path):
    source = write_routing_3mf(
        tmp_path / "sparse.3mf",
        {4: [*SINGLE, {"id": 1, "used_g": 0}, {"id": 2, "type": "PETG", "used_g": "0.0001"}]},
    )
    result = read_print_requirements(source)
    assert result.status == "ok"
    assert [s["slot_id"] for s in result.used_filaments] == [2, 3]
    assert result.used_filaments[0]["color"] is None
    assert result.used_filaments[0]["used_grams"] == 0.0001
    assert result.used_filaments[0]["tray_info_idx"] is None


def test_active_channel_without_material_is_unavailable(tmp_path):
    source = write_routing_3mf(tmp_path / "unknown.3mf", {1: [*SINGLE, {"id": 2, "used_g": 5}]})
    assert read_print_requirements(source).reason == "filament_type_unavailable"


@pytest.mark.parametrize("model", sorted(DUAL_NOZZLE_MODELS))
@pytest.mark.parametrize("plate_id", [1, 4])
def test_dual_bindings_are_plate_scoped_for_every_supported_model(tmp_path, model, plate_id):
    source = write_routing_3mf(
        tmp_path / "dual.3mf",
        {1: mixed_filaments(), 4: mixed_filaments(reverse=True)},
        model=model,
        settings=DUAL_SETTINGS,
    )
    result = read_print_requirements(source, plate_id)
    assert result.status == "ok"
    assert [s["nozzle_id"] for s in result.used_filaments] == ([1, 0] if plate_id == 1 else [0, 1])
    assert result.used_filaments[1]["used_grams"] == 0.0001
    assert result.nozzle_constraints["nozzle_diameter"] == ["0.4", "0.6"]


@pytest.mark.parametrize("model", sorted(DUAL_NOZZLE_MODELS))
def test_known_dual_model_with_missing_bindings_is_not_any_nozzle(tmp_path, model):
    source = write_routing_3mf(tmp_path / "dual.3mf", {1: SINGLE}, model=model)
    result = read_print_requirements(source)
    assert result.reason == "nozzle_mapping_unavailable"
    assert result.used_filaments == ()


def test_dual_file_evidence_also_applies_to_unknown_model(tmp_path):
    source = write_routing_3mf(
        tmp_path / "dual.3mf", {1: SINGLE}, model="Future model", settings={"physical_extruder_map": ["1", "0"]}
    )
    assert read_print_requirements(source).reason == "nozzle_mapping_unavailable"


def test_h2c_group_table_returns_physical_extruder_not_rack_wire_id(tmp_path):
    source = write_routing_3mf(
        tmp_path / "rack.3mf",
        {4: [{**SINGLE[0], "group_id": 8}]},
        model="H2C",
        settings=DUAL_SETTINGS,
        nozzle_groups={8: 2},
    )
    result = read_print_requirements(source)
    assert result.status == "ok"
    assert result.used_filaments[0]["nozzle_id"] == 0


def test_single_active_nozzle_uses_file_configuration(tmp_path):
    source = write_routing_3mf(
        tmp_path / "one-active.3mf",
        {4: SINGLE},
        model="H2D",
        settings={**DUAL_SETTINGS, "extruder_nozzle_stats": ["Standard#0", "Standard#1"]},
    )
    result = read_print_requirements(source)
    assert result.status == "ok"
    assert result.used_filaments[0]["nozzle_id"] == 0


def test_unused_channel_without_group_does_not_erase_active_dual_bindings(tmp_path):
    source = write_routing_3mf(
        tmp_path / "unused.3mf",
        {4: [*mixed_filaments(), {"id": 3, "type": "ABS", "used_g": 0}]},
        model="X2D",
        settings=DUAL_SETTINGS,
    )
    result = read_print_requirements(source)
    assert result.status == "ok"
    assert [s["nozzle_id"] for s in result.used_filaments] == [1, 0]


def test_negative_preference_does_not_use_python_negative_indexing(tmp_path):
    source = write_routing_3mf(
        tmp_path / "negative.3mf",
        {1: [{"id": 1, "type": "PLA", "used_g": 1}]},
        model="H2D",
        settings={**DUAL_SETTINGS, "filament_nozzle_map": ["-1"]},
    )
    assert read_print_requirements(source).reason == "nozzle_mapping_unavailable"


@pytest.mark.parametrize(
    "metadata",
    [
        '<config><plate plate_idx="1"/></config>',
        '<config><plate><metadata key="index" value="bad"/></plate></config>',
        '<config><plate><metadata key="index" value="1"/><metadata key="index" value="2"/></plate></config>',
        '<config><plate><metadata key="index" value="1"/></plate><plate><metadata key="index" value="1"/></plate></config>',
    ],
)
def test_invalid_plate_metadata_is_not_repaired_by_first_plate_fallback(tmp_path, metadata):
    source = tmp_path / "invalid.3mf"
    with zipfile.ZipFile(source, "w") as zf:
        zf.writestr("Metadata/slice_info.config", metadata)
        zf.writestr("Metadata/plate_1.gcode", "; synthetic")
    assert read_print_requirements(source, 1).reason == "invalid_plate_metadata"


def test_ambiguous_gcode_names_refused(tmp_path):
    source = write_routing_3mf(tmp_path / "duplicate.3mf", {1: SINGLE})
    with zipfile.ZipFile(source, "a") as zf:
        zf.writestr("other/plate_1.gcode", "; another file")
    assert read_print_requirements(source).reason == "ambiguous_plate_gcode"


@pytest.mark.parametrize("kind", ["missing", "bad_zip", "bad_xml", "unsliced"])
def test_unreadable_source_is_distinct_from_successful_empty_requirements(tmp_path, kind):
    source = tmp_path / "invalid.3mf"
    if kind == "bad_zip":
        source.write_bytes(b"not a ZIP")
    elif kind in ("bad_xml", "unsliced"):
        with zipfile.ZipFile(source, "w") as zf:
            zf.writestr("Metadata/slice_info.config" if kind == "bad_xml" else "3D/3dmodel.model", "<broken")
    result = read_print_requirements(source)
    assert result.status == "unavailable"
    assert result.reason == ("slice_info_missing" if kind == "unsliced" else "source_unreadable")
    assert result.used_filaments == ()


def test_source_change_during_read_refuses_result(tmp_path, monkeypatch):
    source = write_routing_3mf(tmp_path / "changing.3mf", {1: SINGLE})
    before = SourceIdentity.of(source)
    after = SourceIdentity(before.path, before.size + 1, before.mtime_ns + 1)
    revisions = iter([before, after])
    # ``*, sha256`` mirrors the real signature — a captured source is read with
    # its hash label, and a stub without the keyword would raise TypeError instead.
    monkeypatch.setattr(SourceIdentity, "of", lambda path, *, sha256=None: next(revisions))
    assert read_print_requirements(source).reason == "source_changed"


@pytest.mark.parametrize(
    ("model", "expected"),
    [("C11", "P1P"), ("C12", "P1S"), ("N7", "P2S"), ("N9", "A2L"), ("Bambu Lab P2S", "P2S")],
)
def test_single_nozzle_models_use_shared_model_normalization(tmp_path, model, expected):
    source = write_routing_3mf(tmp_path / "single.3mf", {4: SINGLE}, model=model)
    result = read_print_requirements(source)
    assert result.status == "ok"
    assert result.model == expected


def test_model_is_taken_from_selected_plate(tmp_path):
    source = tmp_path / "mixed-model-metadata.3mf"
    with zipfile.ZipFile(source, "w") as zf:
        zf.writestr(
            "Metadata/slice_info.config",
            '<config><plate><metadata key="index" value="1"/>'
            '<metadata key="printer_model_id" value="C11"/>'
            '<filament id="1" type="PLA" used_g="1"/></plate>'
            '<plate><metadata key="index" value="4"/>'
            '<metadata key="printer_model_id" value="N7"/>'
            '<filament id="2" type="PETG" used_g="2"/></plate></config>',
        )
        zf.writestr("Metadata/plate_1.gcode", "; printer_model = P1P")
        zf.writestr("Metadata/plate_4.gcode", "; printer_model = P2S")
    result = read_print_requirements(source, 4)
    assert result.status == "ok"
    assert result.model == "P2S"
    assert result.used_filaments[0]["type"] == "PETG"
