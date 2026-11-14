"""Is a combination of a product's variant choices one an order may take (spec
product-part-variant-combinations, п. 10–11)?

The jar: weight 1 → options 11/12/13 = 400/600/1200, kind 2 → 21/22 = solid/mesh,
lid 3 → 31/32 = screw/snap. Bodies exist for solid 400 and solid 600 only; the screw
lid fits 400 and 600; the snap lid is not bound to a weight.
"""

from itertools import count

from backend.app.models.product import ProductPart
from backend.app.models.product_variant import ProductVariantOption
from backend.app.services.line_composition import availability
from backend.app.services.product_composition import PlateRecipe, sliced_part_ids
from backend.tests.fixtures.part_bindings import bind

_next_id = count(1)


def part(bound, *, kind="printed", qty=1, ignored=False):
    pid = next(_next_id)
    built = ProductPart(
        id=pid,
        name=f"p{pid}",
        name_key=f"p{pid}",
        kind=kind,
        qty_per_unit=qty,
        ignored=ignored,
        sort_order=0,
    )
    options = [ProductVariantOption(id=oid, group_id=gid) for gid, oids in bound.items() for oid in sorted(oids)]
    return bind(built, *options)


BODY_400_PART = part(bound={1: {11}, 2: {21}})
BODY_600_PART = part(bound={1: {12}, 2: {21}})
LID_SCREW = part(bound={3: {31}, 1: {11, 12}})
LID_SNAP = part(bound={3: {32}})
PARTS = [BODY_400_PART, BODY_600_PART, LID_SCREW, LID_SNAP]
BODY_400 = BODY_400_PART.id
ALL = {p.id for p in PARTS}
MAGNET = part(bound={}, kind="purchased")  # never sliced: its id is not in ALL
PARTS_WITH_PURCHASED_MAGNET = [*PARTS, MAGNET]


def test_available_when_every_discriminating_value_is_named_and_sliced():
    assert availability(PARTS, {1: 11, 2: 21, 3: 31}, sliced=ALL) == []


def test_value_no_kit_part_names_closes_the_combination():
    got = availability(PARTS, {1: 11, 2: 22, 3: 31}, sliced=ALL)  # mesh: no mesh body
    assert [(u.code, u.group_id, u.option_id) for u in got] == [("option_without_part", 2, 22)]
    assert got[0].part_id is None


def test_missing_weight_closes_weight_kind_and_lid():
    got = availability(PARTS, {1: 13, 2: 21, 3: 31}, sliced=ALL)  # 1200 solid screw
    assert {(u.group_id, u.option_id) for u in got} == {(1, 13), (2, 21), (3, 31)}
    assert {u.code for u in got} == {"option_without_part"}


def test_unsliced_printed_part_closes_it_and_a_purchased_one_does_not():
    got = availability(PARTS, {1: 11, 2: 21, 3: 31}, sliced=ALL - {BODY_400})
    assert [u.code for u in got] == ["part_not_sliced"]
    assert (got[0].group_id, got[0].option_id, got[0].part_id) == (None, None, BODY_400)
    assert availability(PARTS_WITH_PURCHASED_MAGNET, {1: 11, 2: 21, 3: 31}, sliced=ALL) == []


def test_descriptive_group_adds_no_option_reason():
    p = part(bound={})  # group 9 binds no part
    assert availability([p], {9: 91}, sliced={p.id}) == []
    assert [u.code for u in availability([p], {9: 91}, sliced=set())] == ["part_not_sliced"]


def test_ignored_and_zero_count_parts_do_not_count():
    assert availability([part(bound={2: {22}}, qty=0)], {2: 21}, sliced=set()) == []
    assert availability([part(bound={2: {22}}, ignored=True)], {2: 21}, sliced=set()) == []


def test_bound_to_every_option_never_fires():  # Review Focus 1
    p = part(bound={1: {11, 12, 13}})
    assert availability([p], {1: 13}, sliced={p.id}) == []


def test_sliced_part_ids_reads_only_sliced_recipes_that_yield_the_part():
    rows = [
        (None, None, PlateRecipe(library_file_id=1, plate_index=0, sliced=True, yield_by_part={1: 2, 2: 0})),
        (None, None, PlateRecipe(library_file_id=2, plate_index=1, sliced=False, yield_by_part={3: 1})),
    ]
    assert sliced_part_ids(rows) == {1}
