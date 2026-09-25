"""Layer height comes from the plate that printed (upstream 7e77bf58).

``project_settings.config`` records the PROJECT's settings and can describe an
earlier process or another plate; the plate's own G-code is what the printer
executes. The header parse read 4 KB — enough for the layer count, not for the
config block that carries ``layer_height`` 14-25 KB in — so a print running at
0.08 archived as 0.2 beside a correct layer count.
"""

from __future__ import annotations

import json
import zipfile

from backend.app.services.archive import ThreeMFParser


def _gcode(layer_height: str, *, padding: int = 20_000) -> str:
    header = "; HEADER_BLOCK_START\n; total layer number: 150\n; HEADER_BLOCK_END\n"
    # The config block is alphabetical: independent_support_layer_height and a
    # long run of other keys come before layer_height, pushing it past 4 KB.
    config = (
        "; CONFIG_BLOCK_START\n"
        "; independent_support_layer_height = 0.3\n"
        + "".join(f"; filler_key_{i:05d} = {i}\n" for i in range(padding // 28))
        + f"; layer_height = {layer_height}\n"
        "; CONFIG_BLOCK_END\n"
    )
    return header + config + "G28\n"


def _three_mf(path, *, project: float, plates: dict[int, str]) -> str:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Metadata/project_settings.config", json.dumps({"layer_height": str(project)}))
        for index, gcode in plates.items():
            zf.writestr(f"Metadata/plate_{index}.gcode", gcode)
    return str(path)


def test_the_plate_gcode_wins_over_the_project_setting(tmp_path):
    path = _three_mf(tmp_path / "a.3mf", project=0.2, plates={1: _gcode("0.08")})
    parsed = ThreeMFParser(path).parse()
    assert parsed["layer_height"] == 0.08
    assert parsed["total_layers"] == 150


def test_the_printed_plate_is_the_one_read(tmp_path):
    path = _three_mf(tmp_path / "b.3mf", project=0.2, plates={1: _gcode("0.2"), 2: _gcode("0.08")})
    parsed = ThreeMFParser(path, plate_number=2).parse()
    assert parsed["layer_height"] == 0.08


def test_a_source_3mf_without_gcode_keeps_the_project_value(tmp_path):
    path = _three_mf(tmp_path / "c.3mf", project=0.16, plates={})
    assert ThreeMFParser(path).parse()["layer_height"] == 0.16


def test_a_key_that_merely_ends_in_layer_height_is_not_read(tmp_path):
    gcode = "; total layer number: 10\n; independent_support_layer_height = 0.3\nG28\n"
    path = _three_mf(tmp_path / "d.3mf", project=0.2, plates={1: gcode})
    assert ThreeMFParser(path).parse()["layer_height"] == 0.2
