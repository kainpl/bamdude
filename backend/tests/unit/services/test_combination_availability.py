"""Is a combination of a product's variant choices one an order may take (spec
product-part-variant-combinations, п. 10–11)?

The jar: weight 1 → options 11/12/13 = 400/600/1200, kind 2 → 21/22 = solid/mesh,
lid 3 → 31/32 = screw/snap. Bodies exist for solid 400 and solid 600 only; the screw
lid fits 400 and 600; the snap lid is not bound to a weight. All three groups are
marked «every value needs its own part» (``JAR_STRICT``) unless a test says otherwise.
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
JAR_STRICT = {1, 2, 3}
MAGNET = part(bound={}, kind="purchased")  # never sliced: its id is not in ALL
PARTS_WITH_PURCHASED_MAGNET = [*PARTS, MAGNET]


def test_available_when_every_discriminating_value_is_named_and_sliced():
    assert availability(PARTS, {1: 11, 2: 21, 3: 31}, sliced=ALL, strict_groups=JAR_STRICT) == []


def test_value_no_kit_part_names_closes_the_combination():
    got = availability(PARTS, {1: 11, 2: 22, 3: 31}, sliced=ALL, strict_groups=JAR_STRICT)  # mesh: no mesh body
    assert [(u.code, u.group_id, u.option_id) for u in got] == [("option_without_part", 2, 22)]
    assert got[0].part_id is None


def test_a_value_no_part_names_at_all_is_the_only_reason():
    # 1200 solid screw: no part anywhere names 1200. Solid and screw are named (by the bodies and
    # the screw lid) — they only have no kit left around them, and are not reported.
    got = availability(PARTS, {1: 13, 2: 21, 3: 31}, sliced=ALL, strict_groups=JAR_STRICT)
    assert [(u.code, u.group_id, u.option_id) for u in got] == [("option_without_part", 1, 13)]


def test_a_second_reason_is_not_hidden_behind_a_value_no_part_names():
    body_solid_400 = part(bound={1: {11}, 2: {21}})
    snap_lid_for_mesh = part(bound={3: {32}, 2: {22}})
    parts = [body_solid_400, snap_lid_for_mesh]
    # 1200 + solid + snap: nothing names 1200, and the snap lid exists for mesh only — with a
    # 1200 body added the snap lid would still be missing, so both are said at once. Solid is
    # named by the body once the weight is taken as met, and is not reported.
    got = availability(parts, {1: 13, 2: 21, 3: 32}, sliced={p.id for p in parts}, strict_groups={1, 2, 3})
    assert [(u.code, u.group_id, u.option_id) for u in got] == [
        ("option_without_part", 1, 13),
        ("option_without_part", 3, 32),
    ]


def test_values_named_somewhere_but_not_in_this_kit_are_all_reported():
    body_400_solid = part(bound={1: {11}, 2: {21}})
    body_600_mesh = part(bound={1: {12}, 2: {22}})
    parts = [body_400_solid, body_600_mesh]
    sliced = {p.id for p in parts}
    # 400 + mesh: 400 and mesh are each named by a body, but no body is both — the kit is empty.
    got = availability(parts, {1: 11, 2: 22}, sliced=sliced, strict_groups={1, 2})
    assert [(u.code, u.group_id, u.option_id) for u in got] == [
        ("option_without_part", 1, 11),
        ("option_without_part", 2, 22),
    ]


def test_nothing_chosen_in_a_marked_group_is_reported_alone():
    # A legacy group without a standard, left unchosen: the missing choice is what is missing.
    got = availability(PARTS, {2: 21, 3: 31}, sliced=ALL, strict_groups=JAR_STRICT)
    assert [(u.code, u.group_id, u.option_id) for u in got] == [("option_without_part", 1, None)]


def test_unsliced_printed_part_closes_it_and_a_purchased_one_does_not():
    got = availability(PARTS, {1: 11, 2: 21, 3: 31}, sliced=ALL - {BODY_400}, strict_groups=JAR_STRICT)
    assert [u.code for u in got] == ["part_not_sliced"]
    assert (got[0].group_id, got[0].option_id, got[0].part_id) == (None, None, BODY_400)
    assert availability(PARTS_WITH_PURCHASED_MAGNET, {1: 11, 2: 21, 3: 31}, sliced=ALL, strict_groups=JAR_STRICT) == []


def test_descriptive_group_adds_no_option_reason():
    p = part(bound={})  # group 9 binds no part
    assert availability([p], {9: 91}, sliced={p.id}, strict_groups={9}) == []
    assert [u.code for u in availability([p], {9: 91}, sliced=set(), strict_groups={9})] == ["part_not_sliced"]


def test_ignored_and_zero_count_parts_do_not_count():
    assert availability([part(bound={2: {22}}, qty=0)], {2: 21}, sliced=set(), strict_groups={2}) == []
    assert availability([part(bound={2: {22}}, ignored=True)], {2: 21}, sliced=set(), strict_groups={2}) == []


def test_bound_to_every_option_never_fires():  # Review Focus 1
    p = part(bound={1: {11, 12, 13}})
    assert availability([p], {1: 13}, sliced={p.id}, strict_groups={1}) == []


# ---------- the mark «every value needs its own part» (owner's decision 2026-11-14) ----------

# The add-on: «Magnet: no (51) / yes (52)», the magnet bound to «yes»; the shade in every kit.
SHADE = part(bound={})
MAGNET_YES = part(bound={5: {52}}, kind="purchased")
LAMP = [SHADE, MAGNET_YES]


def test_an_unmarked_add_on_group_never_closes_its_no():
    assert availability(LAMP, {5: 51}, sliced={SHADE.id}, strict_groups=set()) == []
    assert availability(LAMP, {5: 52}, sliced={SHADE.id}, strict_groups=set()) == []
    # Marked, the same «no» names no part of the kit.
    got = availability(LAMP, {5: 51}, sliced={SHADE.id}, strict_groups={5})
    assert [(u.code, u.group_id, u.option_id) for u in got] == [("option_without_part", 5, 51)]


def test_an_unmarked_group_still_needs_its_kit_sliced():
    got = availability(LAMP, {5: 51}, sliced=set(), strict_groups=set())
    assert [(u.code, u.part_id) for u in got] == [("part_not_sliced", SHADE.id)]


def test_marked_and_unmarked_groups_side_by_side():
    # Only the weight marked: mesh (no mesh body) closes nothing; 400 is named by the screw lid of the kit.
    assert availability(PARTS, {1: 11, 2: 22, 3: 31}, sliced=ALL, strict_groups={1}) == []
    # Kind and lid marked, weight not: 1200 is not asked; solid and screw are named somewhere but
    # no part of this (empty) kit names them — both reported.
    got = availability(PARTS, {1: 13, 2: 21, 3: 31}, sliced=ALL, strict_groups={2, 3})
    assert [(u.code, u.group_id, u.option_id) for u in got] == [
        ("option_without_part", 2, 21),
        ("option_without_part", 3, 31),
    ]
    # A mark on a group no counted part has options in still closes nothing.
    assert availability(PARTS, {1: 11, 2: 21, 3: 31, 9: 91}, sliced=ALL, strict_groups={9}) == []


def test_sliced_part_ids_reads_only_sliced_recipes_that_yield_the_part():
    rows = [
        (None, None, PlateRecipe(library_file_id=1, plate_index=0, sliced=True, yield_by_part={1: 2, 2: 0})),
        (None, None, PlateRecipe(library_file_id=2, plate_index=1, sliced=False, yield_by_part={3: 1})),
    ]
    assert sliced_part_ids(rows) == {1}
