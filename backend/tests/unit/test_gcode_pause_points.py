"""Slicer pauses read from a plate's G-code by the one pass that already reads its layers."""

import zipfile

from backend.app.utils.threemf_tools import (
    extract_layer_filament_usage_from_3mf,
    parse_gcode_layer_filament_usage,
)


def _gcode(layers: int, pauses=(), *, times=True, minutes_per_layer=2, prefix="", suffix="") -> str:
    """A plate printed layer by layer; a pause sits at the start of its layer, as BambuStudio writes it."""
    lines = ["M83", "M620 S0A"]
    if prefix:
        lines.append(prefix)
    total = layers * minutes_per_layer
    if times:
        lines.append(f"M73 P0 R{total}")
    for layer in range(1, layers + 1):
        if times:
            lines.append(f"M73 P{layer} R{total - (layer - 1) * minutes_per_layer}")
        lines.append(f"M73 L{layer}")
        if layer in pauses:
            lines += ["; PAUSE_PRINTING", "M400 U1"]
        lines.append("G1 X10 Y10 E1.0")
    if suffix:
        lines.append(suffix)
    return "\n".join(lines) + "\n"


def _pauses(gcode: str):
    return parse_gcode_layer_filament_usage(gcode).pause_points


def test_one_pause_carries_its_layer_and_the_time_left_there():
    assert _pauses(_gcode(10, pauses={4})) == ((4, 14),)


def test_several_pauses_come_in_print_order():
    assert _pauses(_gcode(10, pauses={3, 7, 9})) == ((3, 16), (7, 8), (9, 4))


def test_a_file_without_pauses_has_none_and_keeps_its_layer_usage():
    scan = parse_gcode_layer_filament_usage(_gcode(5))
    assert scan.pause_points == ()
    assert scan == {1: {0: 1.0}, 2: {0: 2.0}, 3: {0: 3.0}, 4: {0: 4.0}, 5: {0: 5.0}}


def test_a_plain_wait_and_a_filament_change_are_not_pauses():
    gcode = "M83\nM73 L1\nM400\nM400 S2\nM400 P100\nM600\nG1 E1\nM73 L2\nG1 E1\n"
    assert _pauses(gcode) == ()


def test_a_file_without_time_markers_gives_the_layer_only():
    assert _pauses(_gcode(10, pauses={4}, times=False)) == ((4, None),)


def test_the_silent_mode_time_is_not_taken_for_the_normal_one():
    gcode = "M83\nM73 P0 R50\nM73 Q0 S90\nM73 L1\nG1 E1\nM73 Q10 S80\nM73 L2\nM400 U1\nG1 E1\n"
    assert _pauses(gcode) == ((2, 50),)


def test_two_pauses_on_one_layer_are_one_point():
    gcode = "M83\nM73 L1\nG1 E1\nM73 P5 R9\nM73 L2\nM400 U1\nG1 E1\nM73 P6 R8\nM400 U1\nM73 L3\nG1 E1\n"
    assert _pauses(gcode) == ((2, 9),)


def test_case_and_indentation_are_tolerated_and_a_broken_time_keeps_the_last_good_one():
    gcode = "M83\nM73 P0 R30\nM73 P1 Rxx\nM73 L1\nG1 E1\nM73 L2\n   m400 u1\nG1 E1\n"
    assert _pauses(gcode) == ((2, 30),)


def test_a_pause_before_the_first_layer_is_not_a_layer_pause():
    gcode = "M83\nM400 U1\nM73 L1\nG1 E1\nM73 L2\nG1 E1\n"
    assert _pauses(gcode) == ()


def test_absolute_extrusion_loses_the_usage_but_not_the_pauses():
    gcode = "M82\nM73 P0 R20\nM73 L1\nG1 E1\nM73 L2\nM400 U1\nG1 E2\n"
    scan = parse_gcode_layer_filament_usage(gcode)
    assert scan == {}
    assert scan.pause_points == ((2, 20),)


def test_layer_numbers_that_restart_hide_every_pause():
    """Printed object by object, the layer counter starts over for each object."""
    one_object = ["M73 L1", "G1 E1", "M73 L2", "M400 U1", "G1 E1", "M73 L3", "G1 E1"]
    gcode = "\n".join(["M83", "M620 S0A", *one_object, *one_object]) + "\n"
    assert _pauses(gcode) == ()


def test_a_file_that_says_it_prints_by_object_hides_every_pause():
    gcode = _gcode(6, pauses={3}, suffix="; CONFIG_BLOCK_START\n; print_sequence = by object\n; CONFIG_BLOCK_END")
    assert _pauses(gcode) == ()
    by_layer = _gcode(6, pauses={3}, suffix="; print_sequence = by layer")
    assert _pauses(by_layer) == ((3, 8),)


def _three_mf(path, plates: dict[int, str], *, extra: dict[str, str] | None = None):
    with zipfile.ZipFile(path, "w") as archive:
        for number, gcode in plates.items():
            archive.writestr(f"Metadata/plate_{number}.gcode", gcode)
        for name, text in (extra or {}).items():
            archive.writestr(name, text)
    return path


def test_only_the_printed_plate_answers(tmp_path):
    path = _three_mf(tmp_path / "two.3mf", {1: _gcode(8), 2: _gcode(8, pauses={5})})
    assert extract_layer_filament_usage_from_3mf(path, 2).pause_points == ((5, 8),)
    assert extract_layer_filament_usage_from_3mf(path, 1).pause_points == ()


def test_a_plate_the_file_does_not_hold_borrows_no_other_plates_pauses(tmp_path):
    path = _three_mf(tmp_path / "two.3mf", {1: _gcode(8, pauses={2}), 2: _gcode(8, pauses={5})})
    scan = extract_layer_filament_usage_from_3mf(path, 7)
    assert scan  # the usage fallback to the lowest plate is unchanged
    assert scan.pause_points == ()
    # An unknown plate of a multi-plate file names no plate either.
    assert extract_layer_filament_usage_from_3mf(path).pause_points == ()


def test_a_single_plate_file_answers_without_a_plate_number(tmp_path):
    path = _three_mf(tmp_path / "one.3mf", {1: _gcode(8, pauses={5})})
    assert extract_layer_filament_usage_from_3mf(path).pause_points == ((5, 8),)


def test_the_per_layer_custom_gcode_xml_is_not_a_source(tmp_path):
    """Its entries are heights in millimetres and carry no time: only the G-code is read."""
    xml = (
        '<?xml version="1.0" encoding="utf-8"?><custom_gcodes_per_layer><plate><plate_info id="1"/>'
        '<layer top_z="1.2" type="1" extruder="1" color="" extra="" gcode="M400 U1"/>'
        "</plate></custom_gcodes_per_layer>"
    )
    path = _three_mf(tmp_path / "xml.3mf", {1: _gcode(8)}, extra={"Metadata/custom_gcode_per_layer.xml": xml})
    assert extract_layer_filament_usage_from_3mf(path, 1).pause_points == ()
