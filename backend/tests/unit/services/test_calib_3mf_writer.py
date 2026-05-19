"""Tests for calib_3mf_writer — W2 Phase 0 / Phase 1 3MF composer."""

from __future__ import annotations

import io
import json
import struct
import zipfile

import pytest

from backend.app.services.calib_3mf_writer import (
    WRAPPED_OBJECT_ID,
    CustomGcodeItem,
    ObjectOverride,
    write_calibration_3mf,
)


def _synth_minimal_stl() -> bytes:
    """Build a 2-triangle binary STL (a unit quad at z=0)."""
    header = b"BamDude test STL" + b"\x00" * (80 - 16)
    count = struct.pack("<I", 2)
    tri1 = struct.pack("<12fH", 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0)
    tri2 = struct.pack("<12fH", 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 1.0, 0.0, 0)
    return header + count + tri1 + tri2


def _make_minimal_3mf_with_object(object_id: int = WRAPPED_OBJECT_ID) -> bytes:
    """Build a 3MF carrying a model_settings.config + project_settings.config
    with one ``<object>`` and a non-empty project json. Exercises the
    pass-through path."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?>\n<Types/>')
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships/>')
        z.writestr(
            "3D/3dmodel.model",
            '<?xml version="1.0" encoding="UTF-8"?>\n<model><resources/><build/></model>',
        )
        z.writestr(
            "Metadata/model_settings.config",
            (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                "<config>\n"
                f'  <object id="{object_id}">\n'
                '    <metadata key="name" value="seed"/>\n'
                "  </object>\n"
                "</config>\n"
            ),
        )
        z.writestr(
            "Metadata/project_settings.config",
            json.dumps({"upstream_key": "kept"}),
        )
    return buf.getvalue()


# ---------- STL wrapping via pa_pattern.3mf scaffold ----------


def test_stl_wrap_inherits_scaffold_boilerplate():
    """STL input → output mirrors pa_pattern.3mf's structure (scaffold)
    so BS / Orca accept it unconditionally. The scaffold is loaded from
    backend/app/data/calib_assets/pressure_advance/pa_pattern.3mf."""
    out = write_calibration_3mf(
        geometry_bytes=_synth_minimal_stl(),
        geometry_kind="stl",
    )
    with zipfile.ZipFile(io.BytesIO(out), "r") as z:
        names = set(z.namelist())
    # All scaffold entries must survive in the output
    for expected in (
        "[Content_Types].xml",
        "_rels/.rels",
        "3D/3dmodel.model",
        "3D/Objects/Cube_1.model",
        "3D/_rels/3dmodel.model.rels",
        "Metadata/slice_info.config",
        "Metadata/model_settings.config",
        "Metadata/project_settings.config",
        "Metadata/custom_gcode_per_layer.xml",
    ):
        assert expected in names, f"missing scaffold entry: {expected}"


def test_stl_wrap_replaces_mesh_contents():
    """The scaffold's Cube mesh in 3D/Objects/Cube_1.model is replaced
    with our STL-derived geometry. File name stays the same so the
    scaffold's <component p:path> reference keeps working.

    Note: `_load_stl_mesh` auto-centres the mesh (XY bbox centre →
    origin, Z bbox bottom → 0) so any STL is consistently positioned
    for the build-item transform's plate-centre translate. Our synth
    STL spans x=0..1 / y=0..1 / z=0..0; after centring that becomes
    x=-0.5..0.5 / y=-0.5..0.5 / z=0..0."""
    out = write_calibration_3mf(
        geometry_bytes=_synth_minimal_stl(),
        geometry_kind="stl",
    )
    with zipfile.ZipFile(io.BytesIO(out), "r") as z:
        mesh_xml = z.read("3D/Objects/Cube_1.model").decode("utf-8")
    # Auto-centring shifts the synth (x=0..1) by -0.5 → x=-0.5..0.5
    assert '<vertex x="-0.500000"' in mesh_xml
    assert '<vertex x="0.500000"' in mesh_xml
    assert '<vertex x="9' not in mesh_xml  # scaffold's cube range


def test_stl_wrap_neutralises_build_item_transform():
    """Pa_pattern's build <item> carries 0.28× scale. For non-PA-Pattern
    meshes the scale is wrong; the writer patches it to identity scale
    + plate-friendly translate when no per-mode scale is given."""
    out = write_calibration_3mf(
        geometry_bytes=_synth_minimal_stl(),
        geometry_kind="stl",
    )
    with zipfile.ZipFile(io.BytesIO(out), "r") as z:
        top = z.read("3D/3dmodel.model").decode("utf-8")
    # The pa_pattern original has 0.277... — we must NOT keep it
    assert "0.277777778" not in top
    # Identity scale with central translate. Python's f-string emits
    # `90.0` for float-typed defaults; the writer accepts both int/float
    # callers — the on-the-wire BS / Orca parser doesn't care which.
    assert '"1.0 0 0 0 1.0 0 0 0 1.0 90.0 90.0 0.0"' in top


def test_stl_wrap_honors_build_transform_scale():
    """``build_transform_scale`` lets per-mode builders set the
    rendered print size without modifying the mesh — same approach BS
    uses for tower-shape calibrations."""
    out = write_calibration_3mf(
        geometry_bytes=_synth_minimal_stl(),
        geometry_kind="stl",
        build_transform_scale=(0.0625, 0.0625, 0.85),
    )
    with zipfile.ZipFile(io.BytesIO(out), "r") as z:
        top = z.read("3D/3dmodel.model").decode("utf-8")
    assert '"0.0625 0 0 0 0.0625 0 0 0 0.85 90.0 90.0 0.0"' in top


def test_stl_wrap_renames_cube_to_calibration_in_model_settings():
    out = write_calibration_3mf(
        geometry_bytes=_synth_minimal_stl(),
        geometry_kind="stl",
    )
    with zipfile.ZipFile(io.BytesIO(out), "r") as z:
        settings = z.read("Metadata/model_settings.config").decode("utf-8")
    assert 'value="Cube"' not in settings
    assert 'value="calibration"' in settings


def test_stl_wrap_emits_permissive_project_settings():
    """``project_settings.config`` keeps the scaffold's 322 inherited
    keys (BS CLI dereferences ``printer_settings_id`` etc. during
    cross-table lookups and SIGSEGVs if they're missing) with our
    forced overrides on top: empty compat lists, cleared
    ``upward_compatible_machine`` (so the CLI's machine-switch guard
    accepts bundles whose printer isn't in N1's upward list — P1S /
    P1P / X1 / X1C only), brim keys stripped (no auto_brim on tower
    modes), and ``curr_bed_type`` defaulted to the filament-permissive
    plate."""
    out = write_calibration_3mf(
        geometry_bytes=_synth_minimal_stl(),
        geometry_kind="stl",
    )
    with zipfile.ZipFile(io.BytesIO(out), "r") as z:
        project = json.loads(z.read("Metadata/project_settings.config").decode("utf-8"))
    assert project["compatible_printers"] == []
    assert project["compatible_printers_condition"] == ""
    assert project["compatible_prints"] == []
    assert project["print_compatible_printers"] == []
    assert project["upward_compatible_machine"] == []
    # Brim keys are the only identity-adjacent keys we strip.
    assert "brim_type" not in project
    assert "brim_width" not in project
    assert "brim_object_gap" not in project
    # Identity keys are kept so BS CLI / GUI cross-references resolve.
    assert "printer_settings_id" in project
    assert "filament_settings_id" in project


def test_stl_wrap_patches_target_printer_settings_id():
    """``target_printer_settings_id`` overwrites the scaffold's N1
    identity so BS CLI's machine-switch guard sees matching current /
    new printer names and skips the upward-compat path entirely."""
    out = write_calibration_3mf(
        geometry_bytes=_synth_minimal_stl(),
        geometry_kind="stl",
        target_printer_settings_id="Bambu Lab A1 mini 0.4 nozzle",
    )
    with zipfile.ZipFile(io.BytesIO(out), "r") as z:
        project = json.loads(z.read("Metadata/project_settings.config").decode("utf-8"))
    assert project["printer_settings_id"] == "Bambu Lab A1 mini 0.4 nozzle"
    assert project["printer_model"] == "Bambu Lab A1 mini"


def test_stl_wrap_forces_default_curr_bed_type():
    """``curr_bed_type`` defaults to filament-permissive 'Textured PEI
    Plate' so PETG / TPU don't fail plate compatibility validation."""
    out = write_calibration_3mf(
        geometry_bytes=_synth_minimal_stl(),
        geometry_kind="stl",
    )
    with zipfile.ZipFile(io.BytesIO(out), "r") as z:
        project = json.loads(z.read("Metadata/project_settings.config").decode("utf-8"))
    assert project["curr_bed_type"] == "Textured PEI Plate"


def test_stl_wrap_honors_bed_type_override():
    out = write_calibration_3mf(
        geometry_bytes=_synth_minimal_stl(),
        geometry_kind="stl",
        bed_type="Cool Plate",
    )
    with zipfile.ZipFile(io.BytesIO(out), "r") as z:
        project = json.loads(z.read("Metadata/project_settings.config").decode("utf-8"))
    assert project["curr_bed_type"] == "Cool Plate"


def test_stl_wrap_injects_object_overrides():
    out = write_calibration_3mf(
        geometry_bytes=_synth_minimal_stl(),
        geometry_kind="stl",
        object_overrides=[
            ObjectOverride(
                object_id=WRAPPED_OBJECT_ID,
                config={"seam_position": "rear"},
            ),
        ],
    )
    with zipfile.ZipFile(io.BytesIO(out), "r") as z:
        settings = z.read("Metadata/model_settings.config").decode("utf-8")
    assert 'key="seam_position" value="rear"' in settings


def test_stl_wrap_injects_custom_gcode_sorted_and_filtered():
    out = write_calibration_3mf(
        geometry_bytes=_synth_minimal_stl(),
        geometry_kind="stl",
        custom_gcodes=[
            CustomGcodeItem(print_z=0.8, extra="LAST"),
            CustomGcodeItem(print_z=0.2, extra="FIRST"),
            CustomGcodeItem(print_z=-0.5, extra="DROPPED"),
        ],
    )
    with zipfile.ZipFile(io.BytesIO(out), "r") as z:
        xml = z.read("Metadata/custom_gcode_per_layer.xml").decode("utf-8")
    assert xml.index("FIRST") < xml.index("LAST")
    assert "DROPPED" not in xml


def test_stl_wrap_custom_gcode_xml_matches_bs_schema():
    """``<plate>`` is unattributed; an inner ``<plate_info id="1"/>``
    carries the id. ``type`` is the numeric BS enum (4 = Custom).
    ``extruder`` defaults to the uninitialised sentinel. The earlier
    string-typed shape (``<plate id="1">`` with ``type="Custom"``) was
    silently ignored by BS, so no custom gcode landed in the output."""
    out = write_calibration_3mf(
        geometry_bytes=_synth_minimal_stl(),
        geometry_kind="stl",
        custom_gcodes=[CustomGcodeItem(print_z=0.4, extra="M900 K0.01 L1000 M10")],
    )
    with zipfile.ZipFile(io.BytesIO(out), "r") as z:
        xml = z.read("Metadata/custom_gcode_per_layer.xml").decode("utf-8")
    # No attribute on <plate>
    assert "<plate>" in xml
    assert "<plate id=" not in xml
    # plate_info carries the id
    assert '<plate_info id="1"/>' in xml
    # Numeric type code for Custom
    assert 'type="4"' in xml
    # BS's uninitialised extruder sentinel
    assert 'extruder="-858993460"' in xml
    # The actual extra command
    assert "M900 K0.01 L1000 M10" in xml


def test_stl_wrap_layers_project_settings_patch():
    out = write_calibration_3mf(
        geometry_bytes=_synth_minimal_stl(),
        geometry_kind="stl",
        project_settings_patch={"layer_height": "0.2", "custom_calib_key": "hello"},
    )
    with zipfile.ZipFile(io.BytesIO(out), "r") as z:
        project = json.loads(z.read("Metadata/project_settings.config").decode("utf-8"))
    assert project["layer_height"] == "0.2"
    assert project["custom_calib_key"] == "hello"
    # Forced-empty compat still wins over patch
    assert project["compatible_printers"] == []


# ---------- 3MF pass-through ----------


def test_3mf_passthrough_overwrites_metadata_and_keeps_upstream_structure():
    base = _make_minimal_3mf_with_object()
    out = write_calibration_3mf(
        geometry_bytes=base,
        geometry_kind="3mf",
        custom_gcodes=[CustomGcodeItem(print_z=0.4, extra="M900 K0.02 L1000 M10")],
        object_overrides=[
            ObjectOverride(
                object_id=WRAPPED_OBJECT_ID,
                config={"seam_position": "rear"},
            ),
        ],
    )
    with zipfile.ZipFile(io.BytesIO(out), "r") as z:
        gcode = z.read("Metadata/custom_gcode_per_layer.xml").decode("utf-8")
        settings = z.read("Metadata/model_settings.config").decode("utf-8")
        # Upstream 3dmodel.model passes through untouched
        top = z.read("3D/3dmodel.model").decode("utf-8")
        # Upstream project_settings.config remains — no patches asked
        upstream_project = z.read("Metadata/project_settings.config").decode("utf-8")
    assert "<resources/>" in top
    assert "M900 K0.02 L1000 M10" in gcode
    assert 'key="name" value="seed"' in settings  # upstream survived
    assert 'key="seam_position" value="rear"' in settings  # ours layered
    # Without a bed_type / patch, project_settings passes through verbatim
    assert json.loads(upstream_project) == {"upstream_key": "kept"}


def test_3mf_passthrough_patches_project_settings_when_bed_type_given():
    """When the operator picks a bed plate, project_settings.config is
    rewritten with our forced overrides — bed type, empty compat lists,
    cleared ``upward_compatible_machine`` — while preserving upstream's
    full inherited preset chain (BS CLI needs ``printer_settings_id``
    etc. to be non-empty for cross-table dereferences)."""
    base = _make_minimal_3mf_with_object()
    out = write_calibration_3mf(
        geometry_bytes=base,
        geometry_kind="3mf",
        bed_type="High Temp Plate",
    )
    with zipfile.ZipFile(io.BytesIO(out), "r") as z:
        project = json.loads(z.read("Metadata/project_settings.config").decode("utf-8"))
    assert project["curr_bed_type"] == "High Temp Plate"
    assert project["compatible_printers"] == []
    assert project["upward_compatible_machine"] == []
    # Upstream keys flow through (preset chain intact).
    assert project["upstream_key"] == "kept"


# ---------- Defensive ----------


def test_writer_rejects_unknown_geometry_kind():
    with pytest.raises(ValueError, match="geometry_kind"):
        write_calibration_3mf(
            geometry_bytes=b"unused",
            geometry_kind="step",  # type: ignore[arg-type]
        )


# ---------- ObjectOverride.object_name (W2 Phase 7 — Flow Rate) ----------


_FR_SCAFFOLD = """\
<config>
  <object id="2">
    <metadata key="name" value="flowrate_m5"/>
    <metadata key="extruder" value="1"/>
    <part id="1" subtype="normal_part">
      <metadata key="name" value="flowrate_m5"/>
    </part>
  </object>
  <object id="3">
    <metadata key="name" value="flowrate_0"/>
    <metadata key="extruder" value="1"/>
    <part id="2" subtype="normal_part">
      <metadata key="name" value="flowrate_0"/>
    </part>
  </object>
</config>
"""


def test_patch_model_settings_object_name_match_writes_into_correct_block():
    from backend.app.services.calib_3mf_writer import (
        ObjectOverride,
        _patch_model_settings_for_calibration,
    )

    out = _patch_model_settings_for_calibration(
        _FR_SCAFFOLD,
        [
            ObjectOverride(object_name="flowrate_m5", config={"print_flow_ratio": "0.95"}),
            ObjectOverride(object_name="flowrate_0", config={"print_flow_ratio": "1.0"}),
        ],
    )
    obj2 = out[out.index('<object id="2">') : out.index('<object id="3">')]
    obj3 = out[out.index('<object id="3">') :]
    assert '<metadata key="print_flow_ratio" value="0.95"/>' in obj2
    assert '<metadata key="print_flow_ratio" value="1.0"/>' in obj3
    # No cross-contamination between blocks.
    assert '<metadata key="print_flow_ratio" value="1.0"/>' not in obj2
    assert '<metadata key="print_flow_ratio" value="0.95"/>' not in obj3


def test_patch_model_settings_object_name_unknown_raises():
    import pytest

    from backend.app.services.calib_3mf_writer import (
        ObjectOverride,
        _patch_model_settings_for_calibration,
    )

    with pytest.raises(ValueError, match="flowrate_xyz"):
        _patch_model_settings_for_calibration(
            _FR_SCAFFOLD,
            [ObjectOverride(object_name="flowrate_xyz", config={"k": "v"})],
        )


def test_patch_model_settings_object_id_path_still_works():
    from backend.app.services.calib_3mf_writer import (
        ObjectOverride,
        _patch_model_settings_for_calibration,
    )

    out = _patch_model_settings_for_calibration(
        _FR_SCAFFOLD,
        [ObjectOverride(object_id=2, config={"wall_loops": "3"})],
    )
    obj2 = out[out.index('<object id="2">') : out.index('<object id="3">')]
    assert '<metadata key="wall_loops" value="3"/>' in obj2


def test_object_override_requires_exactly_one_of_id_or_name():
    import pytest

    from backend.app.services.calib_3mf_writer import ObjectOverride

    with pytest.raises(ValueError):
        ObjectOverride()  # neither
    with pytest.raises(ValueError):
        ObjectOverride(object_id=2, object_name="flowrate_0")  # both
