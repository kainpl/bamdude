"""``stagger_tag_limits`` / ``stagger_location_limits`` — JSON maps id → cap, normalised on the way in."""

import json

import pytest
from pydantic import ValidationError

from backend.app.schemas.settings import AppSettings, AppSettingsUpdate


def test_the_defaults_are_empty_maps():
    s = AppSettings()
    assert s.stagger_tag_limits == "{}"
    assert s.stagger_location_limits == "{}"


def test_keys_are_sorted_numerically_and_bad_entries_dropped():
    u = AppSettingsUpdate(stagger_tag_limits='{"10": 2, "5": 1, "x": 3, "6": 0, "7": true}')
    assert json.loads(u.stagger_tag_limits) == {"5": 1, "10": 2}
    assert list(json.loads(u.stagger_tag_limits)) == ["5", "10"]


@pytest.mark.parametrize("raw", ["not json", "[1, 2]", "3", "null"])
def test_a_non_object_is_refused(raw):
    with pytest.raises(ValidationError):
        AppSettingsUpdate(stagger_location_limits=raw)


def test_none_passes_through_as_not_provided():
    assert AppSettingsUpdate(stagger_tag_limits=None).stagger_tag_limits is None
