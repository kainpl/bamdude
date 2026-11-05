import zipfile

import pytest
from orca_bamdude.inspector import InspectionError, inspect

SLICE_INFO = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <plate>
    <metadata key="index" value="1"/>
    <metadata key="printer_model_id" value="C12"/>
    <metadata key="nozzle_diameters" value="0.4"/>
    <metadata key="prediction" value="3600"/>
    <metadata key="weight" value="12.5"/>
    <filament id="1" type="PLA" color="#FF0000" used_g="10.0"/>
    <filament id="3" type="PETG" color="#00FF00" used_g="2.5"/>
  </plate>
  <plate>
    <metadata key="index" value="2"/>
    <filament id="2" type="PLA" color="#0000FF" used_g="4"/>
  </plate>
</config>
"""


def _write(path, members: dict, **kwargs):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, **kwargs) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return str(path)


def _sliced(tmp_path, extra=None):
    members = {
        "Metadata/plate_1.gcode": "G28\n" * 100,
        "Metadata/plate_2.gcode": "G28\n" * 10,
        "Metadata/slice_info.config": SLICE_INFO,
        **(extra or {}),
    }
    return _write(tmp_path / "part.gcode.3mf", members)


def test_a_sliced_project_names_its_plates_by_their_index(tmp_path):
    result = inspect(_sliced(tmp_path))

    assert [p.index for p in result.plates] == [1, 2]
    first = result.plate(1)
    assert first.printer_model_id == "C12" and first.prediction_seconds == 3600 and first.weight_g == 12.5
    assert [(f.slot, f.type) for f in first.filaments] == [(1, "PLA"), (3, "PETG")]
    assert result.plate(2).filaments[0].used_g == 4.0


def test_a_project_without_plate_gcode_is_not_sliced(tmp_path):
    path = _write(tmp_path / "model.3mf", {"3D/3dmodel.model": "<model/>", "Metadata/slice_info.config": SLICE_INFO})

    with pytest.raises(InspectionError) as refused:
        inspect(path)
    assert refused.value.code == "not_sliced"


def test_metadata_with_a_dtd_is_refused(tmp_path):
    bomb = '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]><config>&lol;</config>'

    with pytest.raises(InspectionError) as refused:
        inspect(_sliced(tmp_path, {"Metadata/slice_info.config": bomb}))
    assert refused.value.code == "xml_dtd"


def test_an_unsafe_member_name_is_refused(tmp_path):
    with pytest.raises(InspectionError) as refused:
        inspect(_sliced(tmp_path, {"../escape.txt": "x"}))
    assert refused.value.code == "unsafe_member"


def test_a_duplicate_member_is_refused(tmp_path):
    path = tmp_path / "dup.3mf"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Metadata/plate_1.gcode", "G28")
        with pytest.warns(UserWarning):
            archive.writestr("Metadata/plate_1.gcode", "G28")

    with pytest.raises(InspectionError) as refused:
        inspect(str(path))
    assert refused.value.code == "duplicate_member"


def test_a_decompression_bomb_is_refused(tmp_path):
    with pytest.raises(InspectionError) as refused:
        inspect(_sliced(tmp_path, {"Metadata/huge.bin": b"\0" * (8 * 1024 * 1024)}))
    assert refused.value.code == "compression_ratio"


def test_a_damaged_metadata_member_is_refused(tmp_path):
    path = _sliced(tmp_path)
    with open(path, "rb") as handle:
        data = bytearray(handle.read())
    marker = data.find(b"Metadata/slice_info.config")
    # Corrupt the compressed bytes after the local header of the metadata member.
    start = marker + len(b"Metadata/slice_info.config") + 5
    for offset in range(start, start + 40):
        data[offset] ^= 0xFF
    with open(path, "wb") as handle:
        handle.write(bytes(data))

    with pytest.raises(InspectionError) as refused:
        inspect(path)
    assert refused.value.code in ("invalid_zip", "invalid_metadata")


def test_not_a_zip_is_refused(tmp_path):
    path = tmp_path / "x.3mf"
    path.write_bytes(b"not a zip")

    with pytest.raises(InspectionError) as refused:
        inspect(str(path))
    assert refused.value.code == "invalid_zip"
