"""Marker placement, ported from TypeScript and now the only copy.

Each of the four branches is pinned separately: they are chosen by what is
known about the object, so landing in the wrong branch puts a confident marker
on the wrong part — and the operator's next action, skipping it, is
irreversible.
"""

from __future__ import annotations

import pytest

from backend.app.services.plate_markers import marker_position


class TestBranchOneNormalisedCentroid:
    """What the printer's own screen shows — the most trustworthy source."""

    def test_it_is_used_as_a_direct_percentage(self):
        assert marker_position({"x": 0.5, "y": 0.25, "norm": True}, 0, 1, None) == {"x": 50.0, "y": 25.0}

    def test_it_wins_over_a_bbox(self):
        """Order is contract: a normalised centroid outranks millimetres."""
        got = marker_position({"x": 0.5, "y": 0.5, "norm": True}, 0, 1, [0, 0, 1000, 1000])
        assert got == {"x": 50.0, "y": 50.0}

    def test_it_is_clamped_into_the_image(self):
        assert marker_position({"x": -1.0, "y": 9.0, "norm": True}, 0, 1, None) == {"x": 2.0, "y": 98.0}


class TestBranchTwoMillimetresThroughTheBbox:
    def test_the_centre_of_the_bbox_is_the_centre_of_the_image(self):
        assert marker_position({"x": 100.0, "y": 100.0}, 0, 1, [0, 0, 200, 200]) == {"x": 50.0, "y": 50.0}

    def test_the_render_margin_is_honoured(self):
        """The top_N.png render leaves ~8% on each side, so the extremes of the
        bbox land at 8 and 92 rather than at 0 and 100."""
        left = marker_position({"x": 0.0, "y": 100.0}, 0, 1, [0, 0, 200, 200])
        right = marker_position({"x": 200.0, "y": 100.0}, 0, 1, [0, 0, 200, 200])
        assert (left["x"], right["x"]) == (8.0, 92.0)

    def test_the_y_axis_is_inverted_against_the_image(self):
        """⚠️ Image Y grows downward, 3D Y grows toward the BACK of the plate.
        Without the flip, front and back swap — plausibly, and wrongly."""
        front = marker_position({"x": 100.0, "y": 10.0}, 0, 1, [0, 0, 200, 200])
        back = marker_position({"x": 100.0, "y": 190.0}, 0, 1, [0, 0, 200, 200])

        assert front["y"] > back["y"], "an object at the front rendered above one at the back"

    def test_a_degenerate_bbox_falls_through_instead_of_dividing_by_zero(self):
        """A zero-width bbox is not a layout, it is missing data."""
        got = marker_position({"x": 128.0, "y": 128.0}, 0, 1, [50, 50, 50, 50])
        assert got == {"x": 50.0, "y": 50.0}  # branch 3, the 256 mm assumption


class TestBranchThreeAssumedPlate:
    def test_the_middle_of_a_256mm_plate_is_the_middle_of_the_image(self):
        assert marker_position({"x": 128.0, "y": 128.0}, 0, 1, None) == {"x": 50.0, "y": 50.0}

    def test_it_is_inverted_too(self):
        near = marker_position({"x": 128.0, "y": 20.0}, 0, 1, None)
        far = marker_position({"x": 128.0, "y": 230.0}, 0, 1, None)
        assert near["y"] > far["y"]


class TestBranchFourGrid:
    """⚠️ Positions here carry no meaning — only reachability."""

    def test_every_object_gets_a_distinct_spot(self):
        spots = {tuple(marker_position({}, i, 4, None).values()) for i in range(4)}
        assert len(spots) == 4

    def test_it_handles_a_single_object(self):
        got = marker_position({}, 0, 1, None)
        assert 0 < got["x"] < 100 and 0 < got["y"] < 100

    def test_a_partial_coordinate_is_not_a_coordinate(self):
        """x without y cannot place anything; the grid takes it."""
        assert marker_position({"x": 10.0, "y": None}, 0, 2, [0, 0, 200, 200]) == marker_position({}, 0, 2, None)

    def test_it_survives_a_zero_total(self):
        """Callers derive ``total`` from a list; an empty one must not divide
        by zero on the way to rendering nothing."""
        got = marker_position({}, 0, 0, None)
        assert 0 < got["x"] < 100


@pytest.mark.parametrize(
    ("obj", "bbox"),
    [
        ({"x": -50.0, "y": 500.0}, [0, 0, 200, 200]),
        ({"x": 9.9, "y": -9.9, "norm": True}, None),
        ({"x": 9999.0, "y": -1.0}, None),
        ({}, None),
    ],
)
def test_no_input_puts_a_marker_outside_the_image(obj, bbox):
    """Whatever the slicer wrote, the marker stays somewhere it can be seen
    and pressed."""
    got = marker_position(obj, 0, 1, bbox)

    assert 0 < got["x"] < 100 and 0 < got["y"] < 100, (obj, got)


def test_a_bbox_that_is_not_four_numbers_falls_through():
    """⚠️ It is a cached value on client state, not a validated field: absent
    on a fresh reconnect, and whatever the last extractor left otherwise.
    Unpacking it blind made the plate listing answer 500."""
    for bogus in ([1, 2], "0,0,200,200", [None, None, None, None], object()):
        marker = marker_position({"x": 100.0, "y": 50.0}, 0, 1, bogus)
        assert set(marker) == {"x", "y"}, bogus
        # the 256mm-plate branch, not the bbox one
        assert marker == marker_position({"x": 100.0, "y": 50.0}, 0, 1, None), bogus
