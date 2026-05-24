"""Coverage for #1489 — library files display by filename, not the 3MF Title.

Tests the ``_without_print_name`` import helper. The m080 data migration is
covered in ``tests/integration/test_m080_library_drop_print_name_migration.py``.
"""

from backend.app.api.routes.library import _without_print_name


def test_strips_print_name():
    assert _without_print_name({"print_name": "Exported 3D Model", "layers": 42}) == {"layers": 42}


def test_noop_returns_same_object_when_absent():
    m = {"layers": 7}
    assert _without_print_name(m) is m


def test_none_passthrough():
    assert _without_print_name(None) is None


def test_does_not_mutate_input():
    m = {"print_name": "t", "layers": 1}
    _without_print_name(m)
    assert m == {"print_name": "t", "layers": 1}  # original untouched
