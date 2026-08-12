"""Case-insensitive identity for a place.

The same rule spool storage already uses, deliberately: one idea to learn
rather than two for the same problem.
"""

from backend.app.services.printer_location_service import location_key, normalize_location


def test_surrounding_space_is_not_part_of_a_name():
    assert normalize_location("  Shop 2  ") == "Shop 2"


def test_inner_space_is_left_alone():
    assert normalize_location("Shop  2") == "Shop  2"


def test_the_key_ignores_case_and_space():
    assert location_key("  ЦЕХ 2 ") == location_key("цех 2")


def test_the_key_is_not_the_name():
    """The name keeps the operator's capitalisation; only the key is folded."""
    assert normalize_location("Цех 2") == "Цех 2"
    assert location_key("Цех 2") == "цех 2"


def test_nothing_at_all_is_an_empty_name():
    """Callers pass whatever a form gave them; this must not raise."""
    assert normalize_location(None) == ""
    assert location_key(None) == ""
