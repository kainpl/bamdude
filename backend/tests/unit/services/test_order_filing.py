"""Which LINE of an order a plate lands on — pure, no session (spec pass 7, Decision 2).

``line_for_plate`` is the one rule three different doors ask: the candidates
endpoint (which order to propose), the three queue writers (what to stamp on the
row) and ``plan_engine.queued_yield_by_line``'s implicit branch (which line a
line-less row already belongs to). It answers with a line or with nothing — it
never guesses between two lines the plate cannot tell apart, because a guess
here files somebody's print against work nobody ordered.

The session-needing halves (``order_candidates``, ``resolve_line_id``, the
engine's implicit branch) are exercised in ``test_order_candidates_api.py`` and
``test_orders_api.py``, where the rows exist.
"""

from backend.app.models.project_line import ProjectLine
from backend.app.services.order_filing import line_for_plate


def _line(lid, product_id, material=None, sort=0):
    line = ProjectLine(project_id=1, product_id=product_id, quantity=1, material=material, sort_order=sort)
    line.id = lid
    return line


def test_the_only_line_of_that_product_takes_the_plate():
    line = _line(100, 10, material="PETG")
    assert line_for_plate([line], 10, {"PETG"}) is line
    # A line with no material takes every plate — the same reading
    # ``order_metrics`` gives an archive.
    free = _line(101, 10, material=None)
    assert line_for_plate([free], 10, set()) is free


def test_a_line_of_another_product_is_never_the_answer():
    assert line_for_plate([_line(100, 10)], 20, {"PETG"}) is None
    assert line_for_plate([], 10, {"PETG"}) is None


def test_the_material_narrows_two_lines_to_one():
    petg = _line(100, 10, material="PETG")
    pla = _line(101, 10, material="PLA", sort=1)
    assert line_for_plate([petg, pla], 10, {"PETG"}) is petg
    assert line_for_plate([petg, pla], 10, {"PLA"}) is pla


def test_two_lines_the_plate_cannot_tell_apart_resolve_to_nothing():
    """Both accept, so there is no answer — and inventing one files the print
    against the wrong half of the order."""
    a = _line(100, 10, material=None)
    b = _line(101, 10, material=None, sort=1)
    assert line_for_plate([a, b], 10, {"PETG"}) is None
    # Two lines of the SAME material are equally indistinguishable.
    petg_a = _line(102, 10, material="PETG")
    petg_b = _line(103, 10, material="PETG", sort=1)
    assert line_for_plate([petg_a, petg_b], 10, {"PETG"}) is None


def test_a_plate_with_no_materials_narrows_nothing():
    """An unsliced file (or one whose metadata carries no filament) matches no
    CONSTRAINED line at all, exactly as an archive with no filament type does —
    and between two unconstrained lines it still cannot choose."""
    petg = _line(100, 10, material="PETG")
    pla = _line(101, 10, material="PLA", sort=1)
    assert line_for_plate([petg, pla], 10, set()) is None
    assert line_for_plate([petg], 10, set()) is None

    free_a = _line(102, 10, material=None)
    free_b = _line(103, 10, material=None, sort=1)
    assert line_for_plate([free_a, free_b], 10, set()) is None


def test_when_the_material_narrowed_the_set_the_first_line_wins():
    """Three lines, one ruled out by material: the survivors are ordered by
    ``(sort_order, id)`` and the first of them takes the plate."""
    second = _line(100, 10, material=None, sort=5)
    first = _line(101, 10, material="PETG", sort=1)
    ruled_out = _line(102, 10, material="PLA", sort=0)
    assert line_for_plate([second, first, ruled_out], 10, {"PETG"}) is first
