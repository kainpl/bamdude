"""The one reader of a line's composition (spec workshop-product-variants, rules 8, 12, 15)."""

from backend.app.models.product import ProductPart
from backend.app.models.product_variant import ProductVariantOption
from backend.app.services.line_composition import composition, config_key, counted, standard_composition
from backend.tests.fixtures.part_bindings import bind

TAIL = 1  # the one group the options 10 (straight) and 11 (angled) belong to


def _p(pid, qty=1, option=None, kind="printed"):
    part = ProductPart(id=pid, name=f"p{pid}", name_key=f"p{pid}", kind=kind, qty_per_unit=qty, sort_order=0)
    return bind(part, *([ProductVariantOption(id=option, group_id=TAIL)] if option is not None else []))


FLASK, STRAIGHT, ANGLED, SCREW = _p(1, 1), _p(2, 1, option=10), _p(3, 1, option=11), _p(4, 2, kind="purchased")
PARTS = [FLASK, STRAIGHT, ANGLED, SCREW]


def _ids(comp):
    return {(p.id, per) for p, per in comp}


def test_unbound_parts_and_the_chosen_option():
    assert _ids(composition(PARTS, "product", {11}, {})) == {(1, 1), (3, 1), (4, 2)}


def test_an_override_changes_a_count_and_zero_drops_the_part():
    assert _ids(composition(PARTS, "product", {10}, {1: 2, 4: 0})) == {(1, 2), (2, 1)}


def test_an_override_can_bring_in_a_part_of_another_option():
    # An angled-tail line that also wants one straight tail (plan Review Focus 2).
    assert _ids(composition(PARTS, "product", {11}, {2: 1})) == {(1, 1), (2, 1), (3, 1), (4, 2)}


def test_a_part_the_product_zeroes_stays_out_unless_a_count_brings_it():
    zeroed = _p(5, 0)
    assert _ids(composition([*PARTS, zeroed], "product", {10}, {})) == {(1, 1), (2, 1), (4, 2)}
    assert (5, 3) in _ids(composition([*PARTS, zeroed], "product", {10}, {5: 3}))


def test_parts_mode_is_exactly_the_counts():
    assert _ids(composition(PARTS, "parts", set(), {2: 3, 1: 0})) == {(2, 3)}


def test_standard_and_counted():
    assert _ids(standard_composition(PARTS, {10})) == {(1, 1), (2, 1), (4, 2)}
    assert _ids(counted(standard_composition(PARTS, {10}))) == {(1, 1), (2, 1)}


def test_the_key_is_stable_and_standard_without_groups_is_empty():
    assert config_key("product", {}, {}) == ""
    assert config_key("product", {7: 11, 5: 10}, {}) == "5=10;7=11"
    assert config_key("product", {5: 10, 7: 11}, {3: 0, 1: 2}) == "5=10;7=11|1=2;3=0"
    assert config_key("parts", {}, {2: 3, 1: 1}) == "parts:1=1;2=3"
