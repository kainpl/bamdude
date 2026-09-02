from collections import Counter

import pytest

from backend.app.models.product import ProductPart, ProductPlate
from backend.app.services.product_composition import (
    add_alias,
    merge_parts,
    part_index,
    plate_key_counts,
    plate_materials,
    purchased_name_key,
    recipe_for,
    remove_alias,
)

META = {
    "plates": [
        {
            "index": 1,
            "objects": ["bracket.stl", "lid.stl"],
            "printable_objects": {"1": "bracket.stl", "2": "bracket.stl_2", "3": "lid.stl"},
            "print_time_seconds": 3600,
            "filament_used_grams": 40.0,
            "filaments": [
                {"slot_id": 1, "type": "PETG", "color": "#000000"},
                {"slot_id": 2, "type": "petg", "color": "#FFFFFF"},
            ],
        },
        {"index": 2, "objects": ["clip.stl"], "printable_objects": {str(i): f"clip.stl_{i}" for i in range(1, 11)}},
    ]
}


def _part(pid, key, aliases=None, qty=1, kind="printed"):
    p = ProductPart(product_id=1, kind=kind, name=key, name_key=key, qty_per_unit=qty, aliases=aliases or [key])
    p.id = pid
    return p


def test_instances_are_counted_from_printable_objects_not_the_deduplicated_list():
    counts, display = plate_key_counts(META, 1)
    assert counts == Counter({"bracket.stl": 2, "lid.stl": 1})
    assert display["bracket.stl"] == "bracket.stl"
    counts2, _ = plate_key_counts(META, 2)
    assert counts2 == Counter({"clip.stl": 10})


def test_plate_zero_means_the_whole_file():
    counts, _ = plate_key_counts(META, 0)
    assert counts == Counter({"bracket.stl": 2, "lid.stl": 1, "clip.stl": 10})


def test_materials_are_upper_cased_tokens():
    assert plate_materials(META, 1) == {"PETG"}
    assert plate_materials(META, 2) == set()


def test_recipe_resolves_through_aliases_and_reports_unassigned():
    bracket = _part(10, "bracket", aliases=["bracket", "bracket.stl"])
    plate = ProductPlate(product_id=1, library_file_id=5, plate_index=1)
    recipe = recipe_for(plate, META, "gcode", [bracket])
    assert recipe.sliced is True
    assert recipe.yield_by_part == {10: 2}
    assert recipe.unassigned == {"lid.stl": 1}
    assert recipe.print_time_seconds == 3600 and recipe.filament_used_grams == 40.0


def test_unsliced_plate_is_flagged():
    plate = ProductPlate(product_id=1, library_file_id=5, plate_index=2)
    recipe = recipe_for(plate, META, "3mf", [])
    assert recipe.sliced is False and recipe.print_time_seconds is None


def test_merge_unions_aliases_and_keeps_target_qty():
    a, b = _part(1, "bracket", qty=4), _part(2, "bracket_v2", aliases=["bracket_v2", "bracket-old"], qty=9)
    merge_parts(a, b)
    assert a.qty_per_unit == 4 and set(a.aliases) == {"bracket", "bracket_v2", "bracket-old"} and a.auto is False


def test_alias_must_be_unique_within_the_product():
    a, b = _part(1, "bracket"), _part(2, "lid")
    with pytest.raises(ValueError):
        add_alias([a, b], a, "lid")
    add_alias([a, b], a, "bracket.stl")
    assert "bracket.stl" in a.aliases
    with pytest.raises(ValueError):
        remove_alias(a, "bracket")  # a part always keeps its own key
    remove_alias(a, "bracket.stl")
    assert a.aliases == ["bracket"]


def test_part_index_covers_own_key_and_aliases_and_purchased_prefix():
    a = _part(1, "bracket", aliases=["bracket", "bracket.stl"])
    s = _part(2, purchased_name_key("M3 Screw"), kind="purchased")
    idx = part_index([a, s])
    assert idx["bracket.stl"] is a and idx["purchased:m3 screw"] is s


def test_whole_file_totals_sum_only_the_plates_that_carry_a_figure():
    # Top-level keys are ONE plate's snapshot — a half-sliced file must not
    # report plate 1's 3600s as the whole file's time.
    meta = {
        "print_time_seconds": 3600,
        "filament_used_grams": 40.0,
        "plates": [
            {"index": 1, "printable_objects": {"1": "a.stl"}, "print_time_seconds": 3600, "filament_used_grams": 40.0},
            {"index": 2, "printable_objects": {"1": "b.stl"}},
            {"index": 3, "printable_objects": {"1": "c.stl"}, "print_time_seconds": 1800, "filament_used_grams": 10.0},
        ],
    }
    recipe = recipe_for(ProductPlate(product_id=1, library_file_id=5, plate_index=0), meta, "3mf", [])
    assert recipe.print_time_seconds == 5400
    assert recipe.filament_used_grams == 50.0


def test_sliced_asks_the_content_flag_not_the_filename():
    meta = {"plates": [{"index": 1, "printable_objects": {"1": "a.stl"}}]}
    whole = ProductPlate(product_id=1, library_file_id=5, plate_index=0)
    # detect_file_type says "gcode" by filename; a missing flag counts as sliced.
    assert recipe_for(whole, meta, "gcode", []).sliced is True
    assert recipe_for(whole, {**meta, "has_sliced_gcode": False}, "gcode", []).sliced is False
    assert recipe_for(whole, meta, "3mf", []).sliced is False
    # ...and the other half of is_printable(): a "3mf" row the content check
    # found gcode inside IS printable, but only on the strength of the flag.
    assert recipe_for(whole, {**meta, "has_sliced_gcode": True}, "3mf", []).sliced is True
    # One plate of a multi-plate file is decided by its own timing alone.
    one = ProductPlate(product_id=1, library_file_id=5, plate_index=1)
    assert recipe_for(one, meta, "gcode", []).sliced is False
