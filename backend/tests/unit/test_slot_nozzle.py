"""The nozzle an AMS slot feeds: its extruder, diameter and flow type.

A K profile is filed per nozzle — diameter AND flow type (Standard / High Flow),
the identity BambuStudio gives it — so binding one to a slot has to ask about
the nozzle THAT slot feeds. Every binder used to take ``state.nozzles[0]`` for
the diameter whatever the slot, and read a ``nozzle_volume_type`` attribute the
printer state never had, so the flow was "standard" on every machine and a
High Flow calibration was never found (upstream e5a18bf5).
"""

from __future__ import annotations

from types import SimpleNamespace

from backend.app.services.bambu_mqtt import NozzleInfo
from backend.app.utils.slot_nozzle import slot_nozzle


def _state(nozzles, ams_extruder_map=None):
    return SimpleNamespace(nozzles=nozzles, ams_extruder_map=ams_extruder_map or {})


H2D = _state(
    # Index = extruder id: 0 is the right (main) hotend, 1 the left.
    [
        NozzleInfo(nozzle_diameter="0.4", nozzle_flow="standard"),
        NozzleInfo(nozzle_diameter="0.6", nozzle_flow="high_flow"),
    ],
    ams_extruder_map={"0": 1, "1": 0},
)


def test_a_slot_reads_the_nozzle_of_the_extruder_it_feeds():
    left = slot_nozzle(H2D, 0, 2)
    right = slot_nozzle(H2D, 1, 0)

    assert (left.extruder, left.diameter, left.flow) == (1, "0.6", "high_flow")
    assert (right.extruder, right.diameter, right.flow) == (0, "0.4", "standard")


def test_the_external_holder_names_its_side():
    """Ext-L (tray 0) feeds the left hotend, Ext-R (tray 1) the right."""
    assert slot_nozzle(H2D, 255, 0).extruder == 1
    assert slot_nozzle(H2D, 255, 0).flow == "high_flow"
    assert slot_nozzle(H2D, 255, 1).extruder == 0


def test_a_single_nozzle_printer_has_no_extruder_to_name():
    state = _state([NozzleInfo(nozzle_diameter="0.6", nozzle_flow="high_flow"), NozzleInfo()])

    nozzle = slot_nozzle(state, 0, 1)

    assert (nozzle.extruder, nozzle.diameter, nozzle.flow) == (None, "0.6", "high_flow")
    assert nozzle.extruder_or_default == 0
    assert slot_nozzle(state, 255, 0).diameter == "0.6"


def test_an_ams_the_map_does_not_name_reads_the_main_nozzle_and_an_unknown_extruder():
    """0xE (uninitialised, or behind a Filament Track Switch): the extruder is
    unknown — never quietly the right-hand one — and the binders' default of 0
    is theirs to apply."""
    nozzle = slot_nozzle(H2D, 2, 0)

    assert nozzle.extruder is None
    assert nozzle.diameter == "0.4"


def test_a_nozzle_that_reported_nothing_falls_back_to_the_main_one():
    state = _state([NozzleInfo(nozzle_diameter="0.4", nozzle_flow="standard"), NozzleInfo()], {"0": 1})

    nozzle = slot_nozzle(state, 0, 0)

    assert (nozzle.extruder, nozzle.diameter, nozzle.flow) == (1, "0.4", "standard")


def test_an_unreported_flow_is_unknown_and_binds_as_standard():
    """An X1C reports its nozzle by material name ("hardened_steel"), which
    carries no flow class; its calibration rows are Standard."""
    state = _state([NozzleInfo(nozzle_type="hardened_steel", nozzle_diameter="0.4"), NozzleInfo()])

    nozzle = slot_nozzle(state, 0, 0)

    assert nozzle.flow is None
    assert nozzle.flow_or_standard == "standard"


def test_no_state_at_all_is_the_old_defaults():
    nozzle = slot_nozzle(None, 0, 0)

    assert (nozzle.extruder, nozzle.diameter, nozzle.flow) == (None, "0.4", None)
    assert nozzle.diameter_float == 0.4
