"""Verification for the CIEDE2000 metric used to rank spool matches.

The matcher ranks the spools its tolerance admits by how far their colour is
from the one the file asks for. That ranking is only as trustworthy as the
metric, so the formula is pinned against the **published** reference set rather
than against numbers this codebase produced — an implementation that agrees
with 31 independently published values is right; one that agrees with its own
output is merely consistent.

``_ciede2000`` is private and driven directly here on purpose: the reference
data is expressed in L*a*b*, so going through the hex entry point would fold
the sRGB conversion into what is meant to test the difference formula alone.
"""

import math

import pytest

from backend.app.utils.color_utils import _ciede2000, _hex_to_lab, perceptual_color_distance

# Sharma, Wu & Dalal, "The CIEDE2000 Color-Difference Formula", Table 1.
# ⚠️ Pairs 9-12 straddle the hue-angle discontinuity and are what catch a sign
# error in the mean-hue branch; the last two sit near black, where the lightness
# weighting dominates.
SHARMA_PAIRS = [
    ((50.0000, 2.6772, -79.7751), (50.0000, 0.0000, -82.7485), 2.0425),
    ((50.0000, 3.1571, -77.2803), (50.0000, 0.0000, -82.7485), 2.8615),
    ((50.0000, 2.8361, -74.0200), (50.0000, 0.0000, -82.7485), 3.4412),
    ((50.0000, -1.3802, -84.2814), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, -1.1848, -84.8006), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, -0.9009, -85.5211), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, 0.0000, 0.0000), (50.0000, -1.0000, 2.0000), 2.3669),
    ((50.0000, -1.0000, 2.0000), (50.0000, 0.0000, 0.0000), 2.3669),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0009), 7.1792),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0010), 7.1792),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0011), 7.2195),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0012), 7.2195),
    ((50.0000, 2.5000, 0.0000), (50.0000, 0.0000, -2.5000), 4.3065),
    ((50.0000, 2.5000, 0.0000), (73.0000, 25.0000, -18.0000), 27.1492),
    ((50.0000, 2.5000, 0.0000), (61.0000, -5.0000, 29.0000), 22.8977),
    ((50.0000, 2.5000, 0.0000), (56.0000, -27.0000, -3.0000), 31.9030),
    ((50.0000, 2.5000, 0.0000), (58.0000, 24.0000, 15.0000), 19.4535),
    ((50.0000, 2.5000, 0.0000), (50.0000, 3.1736, 0.5854), 1.0000),
    ((50.0000, 2.5000, 0.0000), (50.0000, 3.2972, 0.0000), 1.0000),
    ((50.0000, 2.5000, 0.0000), (50.0000, 1.8634, 0.5757), 1.0000),
    ((50.0000, 2.5000, 0.0000), (50.0000, 3.2592, 0.3350), 1.0000),
    ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
    ((63.0109, -31.0961, -5.8663), (62.8187, -29.7946, -4.0864), 1.2630),
    ((61.2901, 3.7196, -5.3901), (61.4292, 2.2480, -4.9620), 1.8731),
    ((35.0831, -44.1164, 3.7933), (35.0232, -40.0716, 1.5901), 1.8645),
    ((22.7233, 20.0904, -46.6940), (23.0331, 14.9730, -42.5619), 2.0373),
    ((36.4612, 47.8580, 18.3852), (36.2715, 50.5065, 21.2231), 1.4146),
    ((90.8027, -2.0831, 1.4410), (91.1528, -1.6435, 0.0447), 1.4441),
    ((90.9257, -0.5406, -0.9208), (88.6381, -0.8985, -0.7239), 1.5381),
    ((6.7747, -0.2908, -2.4247), (5.8714, -0.0985, -2.2286), 0.6377),
    ((2.0776, 0.0795, -1.1350), (0.9033, -0.0636, -0.5514), 0.9082),
]


class TestAgainstPublishedReference:
    @pytest.mark.parametrize(("lab1", "lab2", "expected"), SHARMA_PAIRS)
    def test_every_reference_pair(self, lab1, lab2, expected):
        assert _ciede2000(lab1, lab2) == pytest.approx(expected, abs=1e-4)

    def test_the_reference_set_is_actually_here(self):
        """If this list ever empties, every assertion above passes vacuously."""
        assert len(SHARMA_PAIRS) == 31


class TestTheMetricItself:
    def test_a_colour_is_zero_from_itself(self):
        assert perceptual_color_distance("1E4821", "1E4821") == pytest.approx(0.0, abs=1e-9)

    def test_it_is_symmetric(self):
        forward = perceptual_color_distance("1E4821", "38202F")
        backward = perceptual_color_distance("38202F", "1E4821")
        assert forward == pytest.approx(backward, abs=1e-12)

    def test_a_leading_hash_is_accepted(self):
        assert perceptual_color_distance("#1E4821", "1E4821") == pytest.approx(0.0, abs=1e-9)

    def test_alpha_is_ignored(self):
        """⚠️ The alpha a slicer writes for a transparent filament is not a
        colour anybody chose, and counting it would stop a transparent filament
        matching itself."""
        assert perceptual_color_distance("1E4821FF", "1E482100") == pytest.approx(0.0, abs=1e-9)

    @pytest.mark.parametrize("bad", ["", None, "12345", "ZZZZZZ"])
    def test_something_unreadable_answers_none_rather_than_a_number(self, bad):
        """A number would be ranked against; None is refused by the caller."""
        assert perceptual_color_distance(bad, "1E4821") is None
        assert perceptual_color_distance("1E4821", bad) is None

    def test_black_and_white_are_the_full_lightness_span(self):
        # White lands at 100.000004, not 100 exactly: the D65 white point is
        # given to five decimals, and that rounding is the whole error. Pinning
        # it tighter would be pinning the constant's rounding, not the maths.
        assert _hex_to_lab("000000")[0] == pytest.approx(0.0, abs=1e-9)
        assert _hex_to_lab("FFFFFF")[0] == pytest.approx(100.0, abs=1e-4)


class TestItRanksLikeAnEyeDoes:
    def test_a_near_shade_beats_a_far_one(self):
        near = perceptual_color_distance("1E4821", "2E5A31")  # green vs green
        far = perceptual_color_distance("1E4821", "38202F")  # green vs purple
        assert near < far

    def test_it_disagrees_with_rgb_where_rgb_is_wrong(self):
        """⚠️ The reason for the whole change, on a measured pair.

        A print asks for a dark green. Two eligible spools sit at the SAME RGB
        distance from it — a tie RGB has no way to break, so the answer falls to
        whichever tray happened to come first. One is a green; the other is a
        dark purple. CIEDE2000 puts the green more than three times nearer.
        """

        def rgb_distance(a: str, b: str) -> float:
            return math.dist(
                [int(a[i : i + 2], 16) for i in (0, 2, 4)],
                [int(b[i : i + 2], 16) for i in (0, 2, 4)],
            )

        required, green, purple = "1E4821", "007040", "301020"
        assert rgb_distance(required, green) == pytest.approx(rgb_distance(required, purple), abs=0.5)

        assert perceptual_color_distance(required, green) < perceptual_color_distance(required, purple) / 3
