"""Pure projections of the cached per-plate metadata into what the library card
pages through (vault 60-specs/library-multiplate-card-spec §4)."""

from backend.app.services.plate_summaries import cached_plates, filament_types_of, plate_summary, split_types


def test_filament_types_keep_slot_order_and_drop_repeats_and_blanks():
    filaments = [
        {"slot_id": 1, "type": "PETG"},
        {"slot_id": 2, "type": " PLA "},
        {"slot_id": 3, "type": "PETG"},
        {"slot_id": 4, "type": ""},
        {"slot_id": 5, "type": None},
        "not a dict",
    ]
    assert filament_types_of(filaments) == ["PETG", "PLA"]
    assert filament_types_of(None) == [] and filament_types_of([]) == []


def test_split_types_reads_the_top_level_snapshot():
    assert split_types("PLA, PETG, PLA") == ["PLA", "PETG"]  # the parser joins with ", "
    assert split_types(None) == [] and split_types("") == [] and split_types(" , ") == []


def test_plate_summary_is_the_seven_fields_coerced():
    plate = {
        "index": 2,
        "name": "",
        "objects": ["a", "b"],
        "object_count": 3,
        "has_thumbnail": 1,
        "print_time_seconds": 900.0,
        "filament_used_grams": 5,
        "total_layers": 20,  # deliberately not carried (spec: no layers on the card)
        "filaments": [{"slot_id": 1, "type": "TPU"}],
        "bed_type": "textured_plate",
    }
    assert plate_summary(plate) == {
        "index": 2,
        "name": None,
        "print_time_seconds": 900,
        "filament_used_grams": 5.0,
        "object_count": 3,
        "filament_types": ["TPU"],
        "has_thumbnail": True,
    }


def test_plate_summary_keeps_unknowns_unknown():
    assert plate_summary({"index": 1}) == {
        "index": 1,
        "name": None,
        "print_time_seconds": None,
        "filament_used_grams": None,
        "object_count": None,
        "filament_types": [],
        "has_thumbnail": False,
    }
    # A bool is not a number: True would otherwise read as 1 second.
    assert plate_summary({"index": 1, "print_time_seconds": True})["print_time_seconds"] is None


def test_cached_plates_is_defensive_about_the_json_column():
    assert cached_plates(None) == [] and cached_plates("x") == [] and cached_plates({"plates": "no"}) == []
    assert cached_plates({"plates": [{"index": 1}, 7, {"index": 2}]}) == [{"index": 1}, {"index": 2}]
