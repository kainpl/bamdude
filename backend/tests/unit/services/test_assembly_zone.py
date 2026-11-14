"""The work zone "Assemble project" packs into, read from a flattened printer preset."""

import pytest

from backend.app.services.assembly.zone import GeometryUnknown, printer_geometry, work_zone

H2C = {
    "printable_area": ["0x0", "330x0", "330x320", "0x320"],
    "printable_height": "325",
    "extruder_printable_area": ["0x0,325x0,325x320,0x320", "25x0,330x0,330x320,25x320"],
    "extruder_printable_height": ["320", "325"],
    "bed_exclude_area": [],
    "wrapping_exclude_area": ["145x310", "251x310", "251x326", "145x326"],
}
P1S = {
    "printable_area": ["0x0", "256x0", "256x256", "0x256"],
    "printable_height": "250",
    "extruder_printable_area": [],
    "extruder_printable_height": [],
    "bed_exclude_area": ["0x0", "18x0", "18x28", "0x28"],
}


def test_h2c_has_two_nozzles_with_their_own_reach_and_height():
    g = printer_geometry(H2C)
    assert g.bed_size == (330.0, 320.0) and [n.index for n in g.nozzles] == [1, 2]
    assert g.nozzles[0].area == (0.0, 0.0, 325.0, 320.0) and g.nozzles[1].area == (25.0, 0.0, 330.0, 320.0)
    assert (g.nozzles[0].max_height, g.nozzles[1].max_height) == (320.0, 325.0)
    assert g.excluded == (((145.0, 310.0), (251.0, 310.0), (251.0, 326.0), (145.0, 326.0)),)


def test_work_zone_is_the_chosen_nozzles_rectangle():
    z = work_zone(printer_geometry(H2C), 2)
    assert (z.width, z.depth, z.max_height) == (330.0, 320.0, 325.0)
    assert list(z.allowed) == [((25.0, 0.0), (330.0, 0.0), (330.0, 320.0), (25.0, 320.0))]
    assert len(z.excluded) == 1


def test_p1s_corner_and_single_nozzle():
    g = printer_geometry(P1S)
    assert len(g.nozzles) == 1 and g.nozzles[0].index == 1 and g.nozzles[0].area == (0.0, 0.0, 256.0, 256.0)
    assert g.nozzles[0].max_height == 250.0 and g.excluded == (((0.0, 0.0), (18.0, 0.0), (18.0, 28.0), (0.0, 28.0)),)
    assert work_zone(g, 1).max_height == 250.0


def test_the_common_profiles_double_rectangle_is_two_polygons():
    flat = dict(P1S, bed_exclude_area=["0x0", "28x0", "28x28", "0x28", "0x28", "8x28", "8x256", "0x256"])
    g = printer_geometry(flat)
    assert g.excluded == (
        ((0.0, 0.0), (28.0, 0.0), (28.0, 28.0), (0.0, 28.0)),
        ((0.0, 28.0), (8.0, 28.0), (8.0, 256.0), (0.0, 256.0)),
    )


def test_trailing_space_in_a_point_is_tolerated():
    flat = {"printable_area": ["0x0", "256x0", "256x256", "0x256 "], "printable_height": "261"}
    assert printer_geometry(flat).bed_size == (256.0, 256.0)


def test_absent_optional_keys_mean_one_nozzle_and_nothing_excluded():
    g = printer_geometry({"printable_area": ["0x0", "180x0", "180x180", "0x180"], "printable_height": "180"})
    assert len(g.nozzles) == 1 and g.excluded == () and g.nozzles[0].max_height == 180.0


def test_a_bed_that_does_not_start_at_the_origin_keeps_its_offset():
    g = printer_geometry({"printable_area": ["10x5", "110x5", "110x105", "10x105"], "printable_height": "100"})
    assert g.bed_size == (110.0, 105.0) and g.nozzles[0].area == (10.0, 5.0, 110.0, 105.0)


@pytest.mark.parametrize(
    "flat",
    [{}, {"printable_area": []}, {"printable_area": ["0x0", "ax0", "1x1"]}, {"printable_area": "0x0,1x0,1x1"},
     {"printable_area": ["0x0", "256x0", "256x256", "0x256"]},
     {"printable_area": ["0x0", "256x0", "256x256", "0x256"], "printable_height": "tall"},
     {"printable_area": ["0x0", "0x0", "0x0"], "printable_height": "10"}],
)  # fmt: skip
def test_missing_or_unreadable_geometry_is_geometry_unknown(flat):
    with pytest.raises(GeometryUnknown):
        printer_geometry(flat)


@pytest.mark.parametrize("nozzle", [0, 3, -1])
def test_unknown_nozzle_is_a_value_error(nozzle):
    with pytest.raises(ValueError):
        work_zone(printer_geometry(H2C), nozzle)


def test_a_bed_whose_origin_lies_inside_it_is_refused_by_name():
    """A centred origin (a delta, some custom machines): parts are laid out from the corner, and such a
    printer's slicer would read the same coordinates as half off the bed."""
    from backend.app.services.assembly.zone import OriginUnsupported

    centred = {"printable_area": ["-110x-110", "110x-110", "110x110", "-110x110"], "printable_height": "200"}
    with pytest.raises(OriginUnsupported):
        printer_geometry(centred)
    assert issubclass(OriginUnsupported, GeometryUnknown)
