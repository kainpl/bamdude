"""Nozzle temperature is the printed plate's first used filament's (upstream 879acdbf1).

``nozzle_temperature`` holds one value per filament of the project. The parser
took the first, so a plate that prints only filament 3 (PETG) showed filament 1's
PLA temperature on its archive.
"""

import json
import zipfile

from backend.app.services.archive import ThreeMFParser

SLICE_INFO = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <plate>
    <metadata key="index" value="1"/>
    <metadata key="prediction" value="600"/>
    <metadata key="weight" value="10"/>
    <filament id="1" type="PLA" color="#FFFFFF" used_g="10"/>
  </plate>
  <plate>
    <metadata key="index" value="2"/>
    <metadata key="prediction" value="900"/>
    <metadata key="weight" value="20"/>
    <filament id="3" type="PETG" color="#000000" used_g="20"/>
  </plate>
</config>
"""


def _threemf(tmp_path, settings: dict, slice_info: str | None = SLICE_INFO):
    path = tmp_path / "model.gcode.3mf"
    with zipfile.ZipFile(path, "w") as zf:
        if slice_info is not None:
            zf.writestr("Metadata/slice_info.config", slice_info)
        zf.writestr("Metadata/project_settings.config", json.dumps(settings))
    return path


TEMPS = {"nozzle_temperature_initial_layer": ["220", "230", "255"], "nozzle_temperature": ["215", "225", "250"]}


def test_the_plate_that_prints_only_filament_three_shows_its_temperature(tmp_path):
    assert ThreeMFParser(_threemf(tmp_path, TEMPS), plate_number=2).parse()["nozzle_temperature"] == 255


def test_the_plate_that_prints_filament_one_keeps_it(tmp_path):
    assert ThreeMFParser(_threemf(tmp_path, TEMPS), plate_number=1).parse()["nozzle_temperature"] == 220


def test_without_slice_info_the_first_value_stands(tmp_path):
    path = _threemf(tmp_path, TEMPS, slice_info=None)
    assert ThreeMFParser(path, plate_number=2).parse()["nozzle_temperature"] == 220


def test_a_filament_the_list_does_not_reach_falls_back_to_the_first(tmp_path):
    path = _threemf(tmp_path, {"nozzle_temperature": ["215"]})
    assert ThreeMFParser(path, plate_number=2).parse()["nozzle_temperature"] == 215


def test_a_scalar_value_is_taken_as_is(tmp_path):
    path = _threemf(tmp_path, {"nozzle_temperature": "240"})
    assert ThreeMFParser(path, plate_number=2).parse()["nozzle_temperature"] == 240
