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

from backend.app.models.product import ProductPart
from backend.app.models.project_line import ProjectLine
from backend.app.services.order_filing import accepting_lines, line_for_plate, lines_counting_plate


def _line(lid, product_id, material=None, sort=0):
    line = ProjectLine(project_id=1, product_id=product_id, quantity=1, material=material, sort_order=sort)
    line.id = lid
    return line


def _part(product_id, key, qty):
    return ProductPart(product_id=product_id, kind="printed", name=key, name_key=key, qty_per_unit=qty, aliases=[key])


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


def test_two_accepting_lines_beside_a_ruled_out_third_still_resolve_to_nothing():
    """⚠️ A ruled-out line does not make the survivors distinguishable.

    This used to return the first survivor by ``(sort_order, id)`` whenever the
    materials had ruled at least one line out — read as "the plate demonstrably
    speaks to this half of the order". It does not: an unrelated PLA line in a
    third position says nothing whatever about the two PETG lines beside it, and
    the exception turned an order nobody could answer into a silent pick.
    """
    first = _line(100, 10, material="PETG", sort=1)
    second = _line(101, 10, material=None, sort=5)
    ruled_out = _line(102, 10, material="PLA", sort=0)
    assert line_for_plate([second, first, ruled_out], 10, {"PETG"}) is None
    # Both accepting lines are still OFFERED — the dialog asks, the writers do
    # not guess, and those are two different questions.
    assert accepting_lines([second, first, ruled_out], 10, {"PETG"}) == [first, second]


def test_accepting_lines_is_ordered_and_filtered_like_the_resolver():
    """The list the candidates endpoint offers is the resolver's own set, in
    ``(sort_order, id)`` order — a line of another product is never in it, and a
    single survivor is exactly what ``line_for_plate`` answers with."""
    petg = _line(100, 10, material="PETG", sort=3)
    free = _line(101, 10, material=None, sort=1)
    pla = _line(102, 10, material="PLA", sort=0)
    other_product = _line(103, 20, material=None, sort=0)

    assert accepting_lines([petg, free, pla, other_product], 10, {"PETG"}) == [free, petg]
    assert accepting_lines([petg, pla, other_product], 10, {"PETG"}) == [petg]
    assert line_for_plate([petg, pla, other_product], 10, {"PETG"}) is petg
    assert accepting_lines([], 10, {"PETG"}) == []


def test_narrowing_keeps_the_lines_whose_product_counts_a_part_of_the_plate():
    """Decision 7: two plate products of one file both hold every plate; only
    the one that COUNTS this plate's parts is the line the print is for."""
    plate1_line, plate2_line = _line(1, 10), _line(2, 20, sort=1)
    parts = {10: [_part(10, "bracket", 2), _part(10, "clip", 0)], 20: [_part(20, "bracket", 0), _part(20, "clip", 10)]}
    assert lines_counting_plate([plate1_line, plate2_line], parts, {"bracket"}) == [plate1_line]
    assert lines_counting_plate([plate1_line, plate2_line], parts, {"clip"}) == [plate2_line]


def test_an_empty_narrowing_keeps_todays_answer():
    a, b = _line(1, 10), _line(2, 20, sort=1)
    parts = {10: [_part(10, "x", 0)], 20: [_part(20, "x", 0)]}
    assert lines_counting_plate([a, b], parts, {"x"}) == [a, b]
    assert lines_counting_plate([a, b], parts, set()) == [a, b]
