"""What the slicer wrote about Filament Track Switch inlets, read back out of the 3MF.

BambuStudio's print dialog recommends which switch inlet each filament should
sit behind (``SelectMachineDialog::get_filament_suggest_pos``) from the slicer's
``optimal_assignment`` and times the difference with a simulator of the shared
inlet channel (``MultiNozzleUtils::simulate_filament_change_time``). Every
input it uses is in the file: ``Metadata/filament_sequence.json`` per plate
(``sequence`` 1-based, or ``filament_sequence`` for a dynamic-map slice;
``nozzle_sequence``; ``optimal_assignment`` indexed by 0-based filament id),
the plate's ``<nozzle id=… extruder_id=…>`` tags in ``slice_info.config``
(extruder written 1-based, ``NozzleInfo::serialize``), and
``machine_load_filament_time`` / ``machine_unload_filament_time`` in
``project_settings.config``. Anything missing means no plan — never a guess.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from backend.app.services.track_switch_plan import read_track_switch_plan

# Measured shape: an X2D three-filament job, filaments 1-2 on one nozzle, 3 on the other.
_SEQUENCE = {
    "plate_1": {"sequence": [1, 2, 3, 2, 1], "nozzle_sequence": [0, 0, 1, 0, 0], "optimal_assignment": [0, 0, 1]}
}
_SLICE_INFO = """<config>
  <plate>
    <metadata key="index" value="1"/>
    <filament id="1" type="PLA" color="#FF0000" used_g="3" used_m="1"/>
    <nozzle id="0" extruder_id="1" nozzle_diameter="0.4" volume_type="Standard"/>
    <nozzle id="1" extruder_id="2" nozzle_diameter="0.4" volume_type="Standard"/>
  </plate>
</config>"""
_SETTINGS = {"machine_load_filament_time": "29", "machine_unload_filament_time": "28"}


def _zip(sequence=_SEQUENCE, slice_info=_SLICE_INFO, settings=_SETTINGS) -> zipfile.ZipFile:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if sequence is not None:
            zf.writestr("Metadata/filament_sequence.json", json.dumps(sequence))
        if slice_info is not None:
            zf.writestr("Metadata/slice_info.config", slice_info)
        if settings is not None:
            zf.writestr("Metadata/project_settings.config", json.dumps(settings))
    buf.seek(0)
    return zipfile.ZipFile(buf)


def test_the_plate_plan_is_read_with_zero_based_filaments_and_extruders():
    plan = read_track_switch_plan(_zip(), 1)

    assert plan == {
        "optimal_assignment": [0, 0, 1],
        "filament_sequence": [0, 1, 2, 1, 0],
        "nozzle_sequence": [0, 0, 1, 0, 0],
        "nozzles": [{"id": 0, "extruder_id": 0}, {"id": 1, "extruder_id": 1}],
        "load_time": 29.0,
        "unload_time": 28.0,
    }


def test_a_dynamic_map_slice_names_its_sequence_differently():
    seq = {"plate_1": {"filament_sequence": [2, 1], "nozzle_sequence": [0, 1], "optimal_assignment": [0, 1]}}

    plan = read_track_switch_plan(_zip(sequence=seq), 1)

    assert plan is not None
    assert plan["filament_sequence"] == [1, 0]


def test_no_plate_named_means_the_only_plate():
    assert read_track_switch_plan(_zip(), None) is not None


def test_no_plate_named_on_a_multi_plate_file_is_no_plan():
    seq = dict(_SEQUENCE, plate_2=_SEQUENCE["plate_1"])
    assert read_track_switch_plan(_zip(sequence=seq), None) is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sequence": None},
        {"sequence": {"plate_1": {"sequence": [], "nozzle_sequence": [], "optimal_assignment": []}}},
        {"sequence": {"plate_1": {"sequence": [1], "nozzle_sequence": [0]}}},
        {"slice_info": "<config><plate><metadata key='index' value='1'/></plate></config>"},
        {"settings": {}},
        {"settings": {"machine_load_filament_time": "0", "machine_unload_filament_time": "0"}},
    ],
    ids=["no-file", "empty-plate", "no-assignment", "no-nozzles", "no-times", "zero-times"],
)
def test_anything_missing_is_no_plan(kwargs):
    assert read_track_switch_plan(_zip(**kwargs), 1) is None


def test_a_plate_the_file_does_not_have_is_no_plan():
    assert read_track_switch_plan(_zip(), 2) is None


def test_a_garbled_file_is_no_plan():
    assert read_track_switch_plan(_zip(sequence={"plate_1": "nonsense"}), 1) is None


# ---------------------------------------------------------------- the routes


@pytest.mark.asyncio
async def test_the_library_route_carries_the_plan(async_client, db_session, tmp_path):
    from backend.app.core.config import settings as app_settings
    from backend.app.models.library import LibraryFile

    storage = tmp_path / "library" / "files"
    storage.mkdir(parents=True)
    path = storage / "Switch.3mf"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Metadata/filament_sequence.json", json.dumps(_SEQUENCE))
        zf.writestr("Metadata/slice_info.config", _SLICE_INFO)
        zf.writestr("Metadata/project_settings.config", json.dumps(_SETTINGS))
    path.write_bytes(buf.getvalue())

    original = app_settings.base_dir
    app_settings.base_dir = tmp_path
    try:
        row = LibraryFile(
            filename="Switch.3mf", file_path=str(path.relative_to(tmp_path)), file_type="3mf", file_size=1
        )
        db_session.add(row)
        await db_session.commit()
        response = await async_client.get(f"/api/v1/library/files/{row.id}/filament-requirements?plate_id=1")
    finally:
        app_settings.base_dir = original

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["track_switch_plan"]["optimal_assignment"] == [0, 0, 1]
    # The existing answer is untouched — print-time matching and the queue read it.
    assert [f["slot_id"] for f in body["filaments"]] == [1]
