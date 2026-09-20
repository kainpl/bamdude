"""The filament-needs core on in-memory rows — pure, no session.

Need per (material, line colour) from plan rows and pending queue rows; shelf
per key from a spool list; the type total always beside a colour figure.
"""

from backend.app.services.filament_needs import (
    FilamentLine,
    NeedKey,
    QueuedNeed,
    SpoolStock,
    colour_matches,
    coloured_index,
    farm_of,
    key_of,
    need_of_plan,
    need_of_queue,
    rows_of,
    stock_by_key,
)
from backend.app.services.plan_engine import LinePlan, OrderPlan, PlanRow


def _plan(rows, materials=None):
    """``rows`` = (line_id, plate_id, count); ``materials`` = line_id → the line's material filter."""
    lines: dict[int, LinePlan] = {}
    for line_id, plate_id, count in rows:
        line = lines.setdefault(
            line_id, LinePlan(line_id=line_id, product_id=1, material=(materials or {}).get(line_id))
        )
        line.rows.append(
            PlanRow(plate_id=plate_id, library_file_id=plate_id, plate_index=0, filename=f"f{plate_id}", count=count)
        )
    return OrderPlan(lines=list(lines.values()))


def test_colour_matches_is_the_one_rule_both_surfaces_share():
    names = lambda h: {"black"} if h == "000000" else set()  # noqa: E731
    assert colour_matches(" Black ", None, "black", names) is True  # by name, casefolded and trimmed
    assert colour_matches(None, "000000", "black", names) is True  # by catalog hex
    assert colour_matches("Blue", "0000ff", "black", names) is False
    assert colour_matches(None, None, "black", names) is False


def test_the_key_normalises_material_and_colour():
    assert key_of("petg", " Чорний ") == NeedKey("PETG", "чорний")
    assert key_of("PLA", None) == NeedKey("PLA", None)
    assert key_of("PLA", "   ") == NeedKey("PLA", None)
    assert key_of(None, "black") is None


def test_one_row_one_filament():
    needs = need_of_plan(_plan([(10, 100, 3)]), {10: "black"}, {100: [FilamentLine("PETG", 12.5)]})
    assert needs.grams == {NeedKey("PETG", "black"): 37.5} and needs.unknown_prints == 0


def test_two_filaments_of_two_types_make_two_keys():
    needs = need_of_plan(
        _plan([(10, 100, 2)]), {10: None}, {100: [FilamentLine("PETG", 10.0), FilamentLine("PLA", 2.0)]}
    )
    assert needs.grams == {NeedKey("PETG", None): 20.0, NeedKey("PLA", None): 4.0}


def test_unknown_grams_are_counted_per_key_and_a_plate_without_filaments_per_order():
    plate_filaments = {100: [FilamentLine("PETG", None)], 200: [], 300: [FilamentLine(None, 5.0)]}
    needs = need_of_plan(_plan([(10, 100, 2), (10, 200, 3), (10, 300, 1)]), {10: None}, plate_filaments)
    assert needs.unknown_by_key == {NeedKey("PETG", None): 2}
    assert needs.unknown_prints == 4  # 3 without filaments + 1 untyped
    assert needs.grams == {}


def test_zero_grams_is_an_answer():
    needs = need_of_plan(_plan([(10, 100, 2)]), {10: None}, {100: [FilamentLine("PETG", 0.0)]})
    assert needs.grams == {NeedKey("PETG", None): 0.0} and needs.unknown_prints == 0


def test_queue_rows_add_their_own_need():
    needs = need_of_queue([QueuedNeed("black", [FilamentLine("PETG", 8.0)]), QueuedNeed(None, None)])
    assert needs.grams == {NeedKey("PETG", "black"): 8.0} and needs.unknown_prints == 1


def test_merge_sums_keys_and_counters():
    a = need_of_plan(_plan([(10, 100, 1)]), {10: None}, {100: [FilamentLine("PETG", 5.0)]})
    b = need_of_queue([QueuedNeed(None, [FilamentLine("PETG", 7.0)]), QueuedNeed(None, None)])
    m = a.merge(b)
    assert m.grams == {NeedKey("PETG", None): 12.0} and m.unknown_prints == 1


SPOOLS = [
    SpoolStock("PETG", "Black", "000000", 800.0),
    SpoolStock("PETG", "Jade White", "ffffff", 950.0),
    SpoolStock("PETG", None, "111111", 300.0),
    SpoolStock("PLA", "Black", None, 400.0),
]


def test_the_colour_narrows_the_shelf_and_the_type_total_rides_beside():
    have = stock_by_key(SPOOLS, [NeedKey("PETG", "black")], lambda _hex: set())
    assert have[NeedKey("PETG", "black")] == (800.0, 2050.0)


def test_without_a_colour_have_equals_the_type_total():
    have = stock_by_key(SPOOLS, [NeedKey("PETG", None)], lambda _hex: set())
    assert have[NeedKey("PETG", None)] == (2050.0, 2050.0)


def test_a_nameless_spool_matches_through_the_catalogue_by_hex():
    names = {"111111": {"black"}}
    have = stock_by_key(SPOOLS, [NeedKey("PETG", "black")], lambda h: names.get(h, set()))
    assert have[NeedKey("PETG", "black")] == (1100.0, 2050.0)


def test_a_colour_nobody_has_reads_zero_not_missing():
    have = stock_by_key(SPOOLS, [NeedKey("PLA", "red")], lambda _hex: set())
    assert have[NeedKey("PLA", "red")] == (0.0, 400.0)


def test_rows_carry_short_and_sort_by_material_then_colour():
    needs = need_of_plan(
        _plan([(10, 100, 1), (11, 200, 1)]),
        {10: "black", 11: None},
        {100: [FilamentLine("PETG", 1000.0)], 200: [FilamentLine("ABS", 50.0)]},
    )
    rows = rows_of(needs, stock_by_key(SPOOLS, needs.keys(), lambda _hex: set()))
    assert [(r.material, r.colour) for r in rows] == [("ABS", None), ("PETG", "black")]
    petg = rows[1]
    assert (petg.need_g, petg.have_g, petg.have_type_g, petg.short_g) == (1000.0, 800.0, 2050.0, 200.0)
    assert rows[0].short_g == 50.0  # nothing on the shelf → short by the whole need


def test_rows_without_a_shelf_leave_the_shelf_figures_none():
    needs = need_of_plan(_plan([(10, 100, 1)]), {10: None}, {100: [FilamentLine("PETG", 10.0)]})
    (row,) = rows_of(needs, None)
    assert (row.need_g, row.have_g, row.have_type_g, row.short_g) == (10.0, None, None, None)


def test_the_farm_sums_one_key_across_orders_and_counts_them():
    a = need_of_plan(_plan([(10, 100, 1)]), {10: "black"}, {100: [FilamentLine("PETG", 100.0)]})
    b = need_of_plan(_plan([(20, 100, 2)]), {20: "black"}, {100: [FilamentLine("PETG", 100.0)]})
    stock = stock_by_key(SPOOLS, [NeedKey("PETG", "black")], lambda _hex: set())
    farm = farm_of({1: rows_of(a, stock), 2: rows_of(b, stock)}, unknown_prints=0, stock_unavailable=False)
    (row,) = farm.rows
    assert (row.need_g, row.have_g, row.orders_count) == (300.0, 800.0, 2) and farm.orders_count == 2


def test_a_gramless_key_still_gets_its_real_shelf_when_stock_covers_needs_keys():
    needs = need_of_plan(_plan([(10, 100, 2)]), {10: "black"}, {100: [FilamentLine("PETG", None)]})
    assert needs.keys() == {NeedKey("PETG", "black")}
    (row,) = rows_of(needs, stock_by_key(SPOOLS, needs.keys(), lambda _hex: set()))
    assert (row.need_g, row.have_g, row.have_type_g, row.short_g, row.unknown_prints) == (0.0, 800.0, 2050.0, 0.0, 2)


def test_a_key_the_stock_dict_does_not_carry_reads_as_an_unknown_shelf_not_zero():
    needs = need_of_plan(_plan([(10, 100, 1)]), {10: None}, {100: [FilamentLine("PETG", 10.0)]})
    (row,) = rows_of(needs, {})
    assert (row.have_g, row.have_type_g, row.short_g) == (None, None, None)


# ---------- the colour goes to ONE filament (spec 2026-09-16 §4) ----------

BODY_AND_SUPPORT = [FilamentLine("PETG", 10.0), FilamentLine("PLA", 2.0)]


def test_the_colour_goes_to_the_heaviest_filament_of_the_lines_material():
    needs = need_of_plan(_plan([(10, 100, 5)], {10: "PETG"}), {10: "black"}, {100: BODY_AND_SUPPORT})
    assert needs.grams == {NeedKey("PETG", "black"): 50.0, NeedKey("PLA", None): 10.0}


def test_the_lines_material_aims_the_colour_at_the_lighter_filament():
    needs = need_of_plan(_plan([(10, 100, 5)], {10: "pla"}), {10: "black"}, {100: BODY_AND_SUPPORT})
    assert needs.grams == {NeedKey("PETG", None): 50.0, NeedKey("PLA", "black"): 10.0}


def test_a_second_filament_of_the_same_type_keeps_no_colour():
    """Body and lettering in one PETG: the lettering is almost surely another colour."""
    plate = [FilamentLine("PETG", 10.0), FilamentLine("PETG", 1.0), FilamentLine("PLA", 2.0)]
    needs = need_of_plan(_plan([(10, 100, 1)], {10: "PETG"}), {10: "black"}, {100: plate})
    assert needs.grams == {NeedKey("PETG", "black"): 10.0, NeedKey("PETG", None): 1.0, NeedKey("PLA", None): 2.0}


def test_without_a_material_the_heaviest_of_the_plate_takes_the_colour():
    needs = need_of_plan(_plan([(10, 100, 5)]), {10: "black"}, {100: BODY_AND_SUPPORT})
    assert needs.grams == {NeedKey("PETG", "black"): 50.0, NeedKey("PLA", None): 10.0}


def test_a_tie_names_nobody():
    two_types = [FilamentLine("PETG", 5.0), FilamentLine("PLA", 5.0)]
    needs = need_of_plan(_plan([(10, 100, 1)]), {10: "black"}, {100: two_types})
    assert needs.grams == {NeedKey("PETG", None): 5.0, NeedKey("PLA", None): 5.0}
    same_type = [FilamentLine("PETG", 5.0), FilamentLine("PETG", 5.0)]
    needs = need_of_plan(_plan([(10, 100, 1)], {10: "PETG"}), {10: "black"}, {100: same_type})
    assert needs.grams == {NeedKey("PETG", None): 10.0}  # both without colour, merged into one key


def test_a_single_gramless_candidate_still_takes_the_colour():
    plate = [FilamentLine("PETG", None), FilamentLine("PLA", 2.0)]
    needs = need_of_plan(_plan([(10, 100, 3)], {10: "PETG"}), {10: "black"}, {100: plate})
    assert needs.unknown_by_key == {NeedKey("PETG", "black"): 3} and needs.grams == {NeedKey("PLA", None): 6.0}


def test_a_gramless_candidate_among_several_names_nobody():
    plate = [FilamentLine("PETG", None), FilamentLine("PETG", 3.0)]
    needs = need_of_plan(_plan([(10, 100, 2)], {10: "PETG"}), {10: "black"}, {100: plate})
    assert needs.unknown_by_key == {NeedKey("PETG", None): 2} and needs.grams == {NeedKey("PETG", None): 6.0}


def test_a_material_the_plate_does_not_carry_colours_nobody():
    needs = need_of_plan(_plan([(10, 100, 1)], {10: "ABS"}), {10: "black"}, {100: BODY_AND_SUPPORT})
    assert needs.grams == {NeedKey("PETG", None): 10.0, NeedKey("PLA", None): 2.0}


def test_queue_rows_follow_the_same_rule():
    needs = need_of_queue([QueuedNeed("black", BODY_AND_SUPPORT, "PLA"), QueuedNeed("black", BODY_AND_SUPPORT)])
    assert needs.grams == {
        NeedKey("PETG", "black"): 10.0,
        NeedKey("PETG", None): 10.0,
        NeedKey("PLA", "black"): 2.0,
        NeedKey("PLA", None): 2.0,
    }


def test_without_a_colour_the_material_changes_nothing():
    needs = need_of_plan(_plan([(10, 100, 2)], {10: "PETG"}), {10: None}, {100: BODY_AND_SUPPORT})
    assert needs.grams == {NeedKey("PETG", None): 20.0, NeedKey("PLA", None): 4.0}


def test_coloured_index_compares_materials_like_the_line_filter():
    plate = [FilamentLine(" petg ", 1.0), FilamentLine("PLA", 9.0), FilamentLine(None, 50.0)]
    assert coloured_index(plate, "PETG") == 0  # spelling-insensitive, like line_accepts_materials
    assert coloured_index(plate, None) == 1  # the untyped 50 g is no candidate at all
    assert coloured_index([], "PETG") is None
    assert coloured_index([FilamentLine("PLA", None)], None) == 0
