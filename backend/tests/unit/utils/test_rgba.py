"""``RRGGBBFF`` or nothing — the one normaliser the schema and the policy both ask."""

import pytest

from backend.app.utils.rgba import normalize_opaque_rgba


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1a2b3cff", "1A2B3CFF"),
        ("#1A2B3CFF", "1A2B3CFF"),
        ("  1a2b3cff  ", "1A2B3CFF"),
        ("000000FF", "000000FF"),
    ],
)
def test_a_colour_is_upper_cased_and_may_carry_one_hash(raw, expected):
    assert normalize_opaque_rgba(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "##1A2B3CFF",  # one '#' is a colour picker's prefix; two is a typo, not a colour
        "1A2B3C",  # no alpha at all
        "1A2B3C80",  # translucent: the firmware compares the alpha byte too
        "1A2B3CFFFF",
        "ZZZZZZFF",
        "#",
    ],
)
def test_anything_that_is_not_an_opaque_rrggbbff_is_refused(raw):
    assert normalize_opaque_rgba(raw) is None


def test_none_is_refused_rather_than_crashing():
    """The policy reads a persisted column that may hold anything at all."""
    assert normalize_opaque_rgba(None) is None
