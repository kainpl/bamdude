"""Bed temperature comes from the plate's own key, not a generic one.

Every archive BamDude has ever written carries ``bed_temperature = NULL``. The
parser looked for ``bed_temperature`` / ``bed_temperature_initial_layer``, which
exist in BambuStudio's config *definitions* and are never written into an
exported 3MF: the value lives per plate type, and ``curr_bed_type`` says which
one applies. Nozzle temperature filled in fine throughout, because
``nozzle_temperature`` is a key that really is written — which is exactly why
only this one field looked broken.

The mapping mirrors BambuStudio's ``get_bed_temp_key`` (``PrintConfig.hpp``)
against the ``curr_bed_type`` enum values (``PrintConfig.cpp``).
"""

import pytest

from backend.app.services.archive import ThreeMFParser


def _settings(bed_type: str | None = None, **temps) -> dict:
    """A project_settings blob shaped like a real one: values are one-element
    string arrays, one per extruder."""
    data: dict = {k: [str(v)] for k, v in temps.items()}
    if bed_type is not None:
        data["curr_bed_type"] = bed_type
    return data


@pytest.mark.parametrize(
    ("bed_type", "expected"),
    [
        ("Cool Plate", 35),
        ("Engineering Plate", 70),
        ("High Temp Plate", 55),
        ("Textured PEI Plate", 75),
        ("Supertack Plate", 45),
    ],
)
def test_each_plate_type_reads_its_own_temperature(bed_type, expected):
    """Values deliberately all different, so picking the wrong key fails rather
    than coincidentally matching. Confirmed against real archived 3MFs: Cool 35,
    High Temp 55, Supertack 45, Textured PEI 75 — each from its own file."""
    data = _settings(
        bed_type,
        cool_plate_temp=35,
        eng_plate_temp=70,
        hot_plate_temp=55,
        textured_plate_temp=75,
        supertack_plate_temp=45,
    )

    assert ThreeMFParser._bed_temperature_from(ThreeMFParser, data) == expected


def test_the_first_layer_value_wins():
    """It is what the operator watches the printer do, and the higher of the two
    on every stock profile."""
    data = _settings("High Temp Plate", hot_plate_temp=55, hot_plate_temp_initial_layer=65)

    assert ThreeMFParser._bed_temperature_from(ThreeMFParser, data) == 65


def test_an_unheated_plate_does_not_read_as_missing():
    """Cool Plate profiles legitimately carry 0. Zero must not send the lookup
    down the fallback path and return some other plate's number."""
    data = _settings("Cool Plate", cool_plate_temp=0, cool_plate_temp_initial_layer=0, hot_plate_temp=75)

    assert ThreeMFParser._bed_temperature_from(ThreeMFParser, data) == 75, (
        "with the chosen plate unheated, the warmest defined plate is the honest fallback"
    )


def test_an_unknown_bed_type_falls_back_to_the_warmest_plate():
    """A guess, and only reached when the alternative is NULL — a number from
    the wrong plate is at least in the right ballpark."""
    data = _settings("Some Future Plate", cool_plate_temp=35, hot_plate_temp=55, textured_plate_temp=75)

    assert ThreeMFParser._bed_temperature_from(ThreeMFParser, data) == 75


def test_a_missing_bed_type_still_yields_something():
    data = _settings(None, hot_plate_temp=55)

    assert ThreeMFParser._bed_temperature_from(ThreeMFParser, data) == 55


def test_a_file_with_no_plate_temperatures_yields_none():
    """The one case that must stay NULL — guessing here would invent data."""
    assert ThreeMFParser._bed_temperature_from(ThreeMFParser, {"nozzle_temperature": ["255"]}) is None


def test_the_old_generic_key_is_not_what_we_read():
    """Pins the actual defect. A file carrying only ``bed_temperature`` is not
    something BambuStudio produces; if this ever starts passing by reading that
    key again, the plate-specific lookup has been lost."""
    data = {"bed_temperature": ["60"], "curr_bed_type": "High Temp Plate", "hot_plate_temp": ["55"]}

    assert ThreeMFParser._bed_temperature_from(ThreeMFParser, data) == 55


def test_values_survive_the_shapes_slicers_actually_write():
    """Arrays, bare numbers and strings all appear in the wild."""
    parse = ThreeMFParser._as_int
    assert parse(["75"]) == 75
    assert parse("75") == 75
    assert parse(75) == 75
    assert parse(75.0) == 75
    assert parse([]) is None
    assert parse(None) is None
    assert parse("not a number") is None
    # A bool is an int in Python; letting it through would record True as 1 °C.
    assert parse(True) is None


# -- the per-filament array: the highest USED entry (upstream c001f596) ------
#
# A plate-temperature key holds one entry per filament, and a 0 means that
# filament cannot print on this plate. The bed has one temperature, so the print
# runs at the highest its filaments ask for — BambuStudio's
# ``GCode::get_highest_bed_temperature`` takes the max over the filaments the
# slice uses. Taking entry 0 stored the wrong plate's value (or none) whenever
# the first filament was not the one printing.


def _parser_with_used(used_slots: list[int] | None) -> ThreeMFParser:
    parser = ThreeMFParser("unused.3mf")
    if used_slots is not None:
        parser.metadata["filament_slots"] = [{"slot_id": slot} for slot in used_slots]
    return parser


TEXTURED = {"curr_bed_type": "Textured PEI Plate"}


def test_the_highest_entry_of_the_used_filaments():
    data = {**TEXTURED, "textured_plate_temp_initial_layer": ["55", "0", "65"]}
    assert _parser_with_used([1, 3])._bed_temperature_from(data) == 65


def test_an_unused_filament_does_not_heat_the_bed():
    data = {**TEXTURED, "textured_plate_temp_initial_layer": ["55", "0", "65"]}
    assert _parser_with_used([1])._bed_temperature_from(data) == 55


def test_a_zero_first_entry_no_longer_hides_the_plates_temperature():
    """Filament 1 cannot print on this plate; filament 2 is the one printing."""
    data = {**TEXTURED, "textured_plate_temp_initial_layer": ["0", "60"], "textured_plate_temp": ["0", "55"]}
    assert _parser_with_used([2])._bed_temperature_from(data) == 60


def test_without_the_used_filaments_every_entry_counts():
    data = {**TEXTURED, "textured_plate_temp_initial_layer": ["55", "70"]}
    assert _parser_with_used(None)._bed_temperature_from(data) == 70


def test_an_array_too_short_for_the_slot_numbers_is_read_whole():
    """One entry per extruder on some exports: slot 3 cannot index it, and must not blank it."""
    data = {**TEXTURED, "textured_plate_temp_initial_layer": ["65"]}
    assert _parser_with_used([3])._bed_temperature_from(data) == 65
