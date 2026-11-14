"""The kit rule over a part's bindings (spec product-part-variant-combinations, п. 5).

A part is in a line's kit when, for EVERY group it has options in, the line chose one of
those options; a part with no bindings is always in it. Groups: weight 1 → options
11/12/13 = 400/600/1200, kind 2 → 21/22 = solid/mesh, lid 3 → 31/32 = screw/snap.
"""

from itertools import count

from backend.app.models.product import ProductPart
from backend.app.models.product_variant import ProductVariantOption
from backend.app.services.line_composition import composition, in_kit
from backend.tests.fixtures.part_bindings import bind

_next_id = count(1)


def part(bound):
    pid = next(_next_id)
    built = ProductPart(id=pid, name=f"p{pid}", name_key=f"p{pid}", kind="printed", qty_per_unit=1, sort_order=0)
    options = [ProductVariantOption(id=oid, group_id=gid) for gid, oids in bound.items() for oid in sorted(oids)]
    return bind(built, *options)


def ids(comp):
    return [p.id for p, _ in comp]


def test_and_across_groups_or_within_a_group():
    body = part(bound={1: {11}, 2: {21}})  # 400 + solid
    lid = part(bound={3: {31}, 1: {11, 12}})  # screw, fits 400 and 600
    plain = part(bound={})  # always
    assert ids(composition([body, lid, plain], "product", {11, 21, 31}, {})) == [body.id, lid.id, plain.id]
    assert ids(composition([body, lid, plain], "product", {13, 21, 31}, {})) == [plain.id]
    assert ids(composition([body, lid, plain], "product", {12, 21, 31}, {})) == [lid.id, plain.id]


def test_bound_to_every_option_of_a_group_is_always_in():
    assert in_kit({1: frozenset({11, 12, 13})}, {12, 21, 31})


def test_count_override_still_brings_a_foreign_part_in():
    body = part(bound={1: {11}})
    assert composition([body], "product", {13}, {body.id: 2}) == [(body, 2)]
