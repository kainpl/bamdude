"""What an archive's energy figure is travels with it (spec plug-meter-watchdog W11).

A Git backup carries ``energy_kwh`` as a number; without its record an estimate would come back
reading as a measurement, and an unknown figure as no energy at all.
"""

from backend.app.services.meter_watch import energy_record, energy_wire_fields, with_energy_record

ESTIMATED = {
    "energy_provenance": {"kind": "estimated", "coverage": 0.97, "final": True},
    "energy_is_approximate": True,
    "energy_reconstructed_from": "power_history",
}


def test_the_record_is_only_the_energy_keys():
    extra = {**ESTIMATED, "printable_objects": {"1": "a"}, "swap": {"x": 1}}

    assert energy_record(extra) == ESTIMATED
    assert energy_record(None) == {}


def test_a_restore_makes_the_energy_keys_exactly_the_backups():
    local = {"energy_is_approximate": True, "notes": "kept"}

    restored = with_energy_record(local, ESTIMATED)

    assert restored == {**ESTIMATED, "notes": "kept"}
    # A backup that says nothing of the kind clears a local claim: overwrite means "match the backup".
    assert with_energy_record(restored, {}) == {"notes": "kept"}


def test_a_malformed_record_is_left_out_not_trusted():
    junk = {
        "energy_provenance": {"kind": "made_up"},
        "energy_is_approximate": "yes",
        "energy_reconstructed_from": "guesswork",
        "unrelated": 1,
    }

    assert with_energy_record({"notes": "kept"}, junk) == {"notes": "kept"}
    assert with_energy_record(None, "not a dict") == {}


def test_the_wire_fields_read_the_record_not_the_number():
    assert energy_wire_fields(ESTIMATED, 0.2) == {
        "energy_status": "estimated",
        "energy_coverage": 0.97,
        "energy_reason": "meter_stalled",
    }
    unknown = {"energy_provenance": {"kind": "unavailable", "unavailable_reason": "source_changed"}}
    assert energy_wire_fields(unknown, None)["energy_status"] == "unavailable"
    assert energy_wire_fields(unknown, None)["energy_reason"] == "source_changed"
    assert energy_wire_fields({"energy_is_approximate": True}, 0.4)["energy_status"] == "approximate"
    assert energy_wire_fields({}, 0.0)["energy_status"] == "measured"
    assert energy_wire_fields(None, None)["energy_status"] is None
    assert energy_wire_fields("garbage", 0.1)["energy_status"] == "measured"
