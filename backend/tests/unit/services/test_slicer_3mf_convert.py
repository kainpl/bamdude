"""Tests for slicer_3mf_convert helpers (cross-printer / multi-plate re-slice)."""

from __future__ import annotations

import zipfile
from io import BytesIO

from backend.app.services.slicer_3mf_convert import (
    count_plates_in_3mf,
    extract_source_printer_model,
    merge_plate_3mfs,
    substitute_unused_plate_filaments,
)


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


# ── count_plates_in_3mf ─────────────────────────────────────────────────────


def test_count_plates_counts_plater_ids():
    model_settings = (
        b'<config><plate><metadata key="plater_id" value="1"/></plate>'
        b'<plate><metadata key="plater_id" value="2"/></plate></config>'
    )
    z = _zip({"Metadata/model_settings.config": model_settings})
    assert count_plates_in_3mf(z) == 2


def test_count_plates_no_metadata_returns_zero():
    assert count_plates_in_3mf(_zip({"3D/3dmodel.model": b"<model/>"})) == 0
    assert count_plates_in_3mf(b"not a zip") == 0


# ── extract_source_printer_model ────────────────────────────────────────────


def test_extract_source_printer_model_canonicalises():
    z = _zip({"Metadata/project_settings.config": b'{"printer_model": "Bambu Lab X1 Carbon"}'})
    assert extract_source_printer_model(z) == "X1C"


def test_extract_source_printer_model_missing():
    assert extract_source_printer_model(_zip({"Metadata/project_settings.config": b"{}"})) is None
    assert extract_source_printer_model(b"not a zip") is None


# ── substitute_unused_plate_filaments ───────────────────────────────────────


def test_substitute_noop_when_plate_none_or_short():
    assert substitute_unused_plate_filaments(b"", None, ["a", "b"]) == ["a", "b"]
    assert substitute_unused_plate_filaments(b"", 0, ["only"]) == ["only"]


def test_substitute_noop_when_no_plate_extruder_metadata():
    # A 3MF with no model_settings → extract returns empty set → fail-open.
    z = _zip({"3D/3dmodel.model": b"<model/>"})
    assert substitute_unused_plate_filaments(z, 0, ["pla", "abs", "petg"]) == ["pla", "abs", "petg"]


def test_substitute_replaces_unused_slots_with_slot1():
    # Plate 0 uses only extruder 1; slots 2 and 3 are unused → become slot 1's.
    model_settings = (
        b"<config>"
        b'<object id="1"><metadata key="extruder" value="1"/></object>'
        b'<plate><metadata key="plater_id" value="0"/>'
        b'<model_instance><metadata key="object_id" value="1"/></model_instance></plate>'
        b"</config>"
    )
    z = _zip({"Metadata/model_settings.config": model_settings})
    out = substitute_unused_plate_filaments(z, 0, ["pla", "abs", "petg"])
    assert out == ["pla", "pla", "pla"]


# ── merge_plate_3mfs ────────────────────────────────────────────────────────


def test_merge_single_plate_passthrough():
    blob = _zip({"Metadata/plate_1.gcode": b"G1"})
    assert merge_plate_3mfs([(1, blob)]) == blob


def test_merge_empty_raises():
    import pytest

    with pytest.raises(ValueError):
        merge_plate_3mfs([])


def test_merge_overlays_per_plate_artifacts():
    base = _zip(
        {
            "Metadata/slice_info.config": b"<config><plate>P1</plate></config>",
            "Metadata/plate_1.gcode": b"G1",
            "3D/3dmodel.model": b"<model/>",
        }
    )
    second = _zip(
        {
            "Metadata/slice_info.config": b"<config><plate>P2</plate></config>",
            "Metadata/plate_2.gcode": b"G2",
        }
    )
    merged = merge_plate_3mfs([(1, base), (2, second)])
    with zipfile.ZipFile(BytesIO(merged), "r") as zf:
        names = set(zf.namelist())
        assert "Metadata/plate_1.gcode" in names
        assert "Metadata/plate_2.gcode" in names  # overlaid from the second plate
        info = zf.read("Metadata/slice_info.config").decode()
        assert "P1" in info and "P2" in info  # both blocks reassembled
