"""A started print takes its expectation whole — archive, time and creator.

The creator used to be left behind on every path but one, where no sweep could
reach it, so a later print of the same name was attributed to the user who had
queued the earlier one (upstream dad7fea37, EP-2).
"""

import pytest

from backend.app import main


@pytest.fixture(autouse=True)
def _clean():
    stores = (main._expected_prints, main._expected_print_registered_at, main._expected_print_creators)
    saved = [dict(s) for s in stores]
    for s in stores:
        s.clear()
    yield
    for s, old in zip(stores, saved, strict=True):
        s.clear()
        s.update(old)


KEYS = [(7, "part.gcode.3mf"), (7, "part"), (7, "part.3mf")]


def test_the_archive_and_the_creator_come_out_together():
    main.register_expected_print(7, "part.gcode.3mf", 42, created_by_id=5)

    assert main._take_expected_print(KEYS) == (42, 5)


def test_nothing_of_the_expectation_is_left_behind():
    main.register_expected_print(7, "part.gcode.3mf", 42, created_by_id=5)

    main._take_expected_print(KEYS)

    for store in (main._expected_prints, main._expected_print_registered_at, main._expected_print_creators):
        assert not [k for k in KEYS if k in store]
    # What is left is an alias the start does not name; it still has its
    # registration time, so the TTL sweep reaches it.
    assert set(main._expected_print_creators) <= set(main._expected_print_registered_at)


def test_a_creator_without_an_expected_archive_is_cleared_too():
    """The path that used to leak: the archive entry is already gone."""
    main._expected_print_creators[(7, "part")] = 5

    assert main._take_expected_print(KEYS) == (None, 5)
    assert (7, "part") not in main._expected_print_creators


def test_a_later_print_of_the_same_name_has_no_inherited_creator():
    main.register_expected_print(7, "part.gcode.3mf", 42, created_by_id=5)
    main._take_expected_print(KEYS)
    main.register_expected_print(7, "part.gcode.3mf", 43)

    assert main._take_expected_print(KEYS) == (43, None)


def test_another_printer_is_untouched():
    main.register_expected_print(8, "part.gcode.3mf", 99, created_by_id=3)

    assert main._take_expected_print(KEYS) == (None, None)
    assert main._take_expected_print([(8, "part.gcode.3mf")]) == (99, 3)
