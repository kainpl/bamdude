"""The H2C names a nozzle by rack position, not by extruder index.

Ported from upstream #2800 (`ec26cba9` + `e9d9e51a` + `dfeac792`). We have no
H2C to test against, so the hardware-derived constants come from upstream's A/B
on a real machine — which they had to run twice, because the first attempt got
**both** of them wrong:

- rack = extruder **1** (not 0). `[17, -1, -1, 1]` printed several millimetres
  above the bed.
- fixed hotend = physical ID **1**, not its extruder index 0. `[0, -1, -1, 17]`
  was rejected by the printer outright.
- `[1, -1, -1, 17]` cleaned, levelled and printed correctly on both nozzles,
  and agrees with three native BambuStudio captures from the same machine.

⚠️ The failure mode is asymmetric, and that shapes every branch here: **omitting
the field costs only the firmware's own nozzle pick, while a wrong physical ID
levels with one hotend and prints with another.** So everything unresolvable
returns None rather than guessing.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from backend.app.services.bambu_mqtt import resolve_rack_nozzle_mapping
from backend.app.utils.printer_models import is_nozzle_rack_model
from backend.app.utils.threemf_tools import (
    extract_nozzle_mapping_from_3mf,
    extract_slot_extruders_from_3mf,
)

RACK_ID = 17
WIRE_SLOTS = 32


def _wire(**slots: int) -> list[int]:
    """The 32-long array with the named 0-based positions filled in."""
    out = [-1] * WIRE_SLOTS
    for index, value in slots.items():
        out[int(index.lstrip("s"))] = value
    return out


class TestTheHardwareVerifiedAnswer:
    def test_a_mixed_plate_dispatches_both_carriages_by_physical_id(self):
        """The exact case the reporter ran: slot 1 fixed, slot 4 on the rack."""
        assert resolve_rack_nozzle_mapping([0, -1, -1, 1], RACK_ID) == _wire(s0=1, s3=RACK_ID)

    def test_the_rack_is_extruder_one(self):
        """Reversing this is what printed millimetres above the bed."""
        resolved = resolve_rack_nozzle_mapping([0, 1], RACK_ID)
        assert resolved[0] == 1, "extruder 0 is the FIXED carriage"
        assert resolved[1] == RACK_ID, "extruder 1 is the rack"

    def test_the_fixed_hotend_answers_to_physical_id_not_its_index(self):
        """Forwarding the index produced a command the printer rejected."""
        assert resolve_rack_nozzle_mapping([0, 1], RACK_ID)[0] == 1

    @pytest.mark.parametrize("rack_id", [16, 17, 18, 19, 20, 21])
    def test_every_rack_position_the_firmware_reports_is_accepted(self, rack_id):
        assert resolve_rack_nozzle_mapping([1], rack_id) == _wire(s0=rack_id)


class TestWhenItRefusesToGuess:
    def test_no_live_rack_position_means_no_mapping(self):
        """Mid-swap or a stale connection. The firmware picks instead."""
        assert resolve_rack_nozzle_mapping([0, 1], None) is None

    def test_a_position_outside_the_rack_range_is_refused(self):
        assert resolve_rack_nozzle_mapping([0, 1], 5) is None

    def test_a_plate_that_never_touches_the_rack_sends_nothing(self):
        """BambuStudio omits nozzle_mapping entirely for a fixed-only plate, so
        this matches it rather than naming a nozzle it need not name."""
        assert resolve_rack_nozzle_mapping([0, 0, -1], RACK_ID) is None

    def test_a_third_carriage_is_refused_rather_than_forwarded(self):
        """An H2C has two. A third index means the file was mapped for another
        machine, and forwarding it raw would name a nozzle that does not exist."""
        assert resolve_rack_nozzle_mapping([1, 2], RACK_ID) is None

    def test_more_slots_than_the_wire_carries(self):
        assert resolve_rack_nozzle_mapping([1] * (WIRE_SLOTS + 1), RACK_ID) is None

    @pytest.mark.parametrize("bad", [None, "x", [], [1, "2"], [1, None, 0]])
    def test_junk_never_raises(self, bad):
        """The only caller publishes an MQTT command with no handler above it,
        and the queue item is already committed as printing."""
        result = resolve_rack_nozzle_mapping(bad, RACK_ID)
        assert result is None or isinstance(result, list)

    def test_a_bool_is_not_an_extruder_index(self):
        """bool subclasses int and would serialise as JSON `true` on the wire."""
        assert resolve_rack_nozzle_mapping([True, 1], RACK_ID) is None
        assert resolve_rack_nozzle_mapping([1], True) is None

    def test_none_inside_the_list_means_slot_not_printed(self):
        assert resolve_rack_nozzle_mapping([None, 1], RACK_ID) == _wire(s1=RACK_ID)


class TestTheModelGate:
    @pytest.mark.parametrize("model", ["H2C", "O1C", "O1C2", "h2c", " H2C "])
    def test_the_rack_models(self, model):
        assert is_nozzle_rack_model(model) is True

    @pytest.mark.parametrize("model", ["H2D", "H2DPRO", "X2D", "P1S", None, ""])
    def test_everything_else_is_untouched(self, model):
        """⚠️ Load-bearing: on every other dual-nozzle printer the mapping values
        ARE extruder indices, and translating them would break H2D."""
        assert is_nozzle_rack_model(model) is False


def _threemf(project: str, slice_info: str) -> zipfile.ZipFile:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Metadata/project_settings.config", project)
        zf.writestr("Metadata/slice_info.config", slice_info)
    return zipfile.ZipFile(buf)


PROJECT_TWO_EXTRUDERS = '{"physical_extruder_map": [0, 1]}'


class TestTheGroupTable:
    def test_a_rack_group_resolves_through_the_files_own_table(self):
        """⚠️ The whole point: on a rack machine the slicer writes a group per
        NOZZLE, not per carriage, so a plate carries more groups than the
        machine has extruders."""
        slice_info = """<config><plate>
          <metadata key="index" value="1"/>
          <nozzle id="0" extruder_id="1"/>
          <nozzle id="1" extruder_id="2"/>
          <nozzle id="2" extruder_id="2"/>
          <filament id="1" group_id="0"/>
          <filament id="2" group_id="1"/>
          <filament id="3" group_id="2"/>
        </plate></config>"""

        assert extract_nozzle_mapping_from_3mf(_threemf(PROJECT_TWO_EXTRUDERS, slice_info)) == {1: 0, 2: 1, 3: 1}

    def test_without_a_table_the_group_is_the_extruder_index(self):
        """Every H2D slice. Changing this would send dual-nozzle AMS matches to
        the wrong extruder."""
        slice_info = """<config><plate>
          <metadata key="index" value="1"/>
          <filament id="1" group_id="0"/>
          <filament id="2" group_id="1"/>
        </plate></config>"""

        assert extract_nozzle_mapping_from_3mf(_threemf(PROJECT_TWO_EXTRUDERS, slice_info)) == {1: 0, 2: 1}

    def test_an_unplaceable_group_drops_the_WHOLE_mapping(self):
        """Half an answer reaches the wire as -1 for the missing slot, and
        against an ams_mapping that DOES name a tray the firmware refuses the
        job outright with HMS 0500-4047."""
        slice_info = """<config><plate>
          <metadata key="index" value="1"/>
          <filament id="1" group_id="0"/>
          <filament id="2" group_id="7"/>
        </plate></config>"""

        assert extract_nozzle_mapping_from_3mf(_threemf(PROJECT_TWO_EXTRUDERS, slice_info)) is None

    def test_grouping_some_filaments_and_not_others_is_unplaceable(self):
        slice_info = """<config><plate>
          <metadata key="index" value="1"/>
          <filament id="1" group_id="0"/>
          <filament id="2"/>
        </plate></config>"""

        assert extract_nozzle_mapping_from_3mf(_threemf(PROJECT_TWO_EXTRUDERS, slice_info)) is None

    def test_plates_disagreeing_about_a_group_fall_back_to_the_index(self):
        slice_info = """<config>
          <plate><metadata key="index" value="1"/><nozzle id="0" extruder_id="1"/>
            <filament id="1" group_id="0"/></plate>
          <plate><metadata key="index" value="2"/><nozzle id="0" extruder_id="2"/>
            <filament id="1" group_id="0"/></plate>
        </config>"""

        assert extract_nozzle_mapping_from_3mf(_threemf(PROJECT_TWO_EXTRUDERS, slice_info)) == {1: 0}


class TestThePlateIsScoped:
    def test_the_named_plate_decides(self):
        """⚠️ A multi-plate file carries one filament list per plate and they
        need not agree — without scoping, a slot takes its extruder from
        whichever plate came last."""
        slice_info = """<config>
          <plate><metadata key="index" value="1"/><filament id="1" group_id="0"/></plate>
          <plate><metadata key="index" value="2"/><filament id="1" group_id="1"/></plate>
        </config>"""

        zf = _threemf(PROJECT_TWO_EXTRUDERS, slice_info)
        assert extract_nozzle_mapping_from_3mf(zf, plate_id=1) == {1: 0}
        assert extract_nozzle_mapping_from_3mf(zf, plate_id=2) == {1: 1}


class TestTheDenseForm:
    def test_a_gap_becomes_minus_one(self):
        slice_info = """<config><plate>
          <metadata key="index" value="1"/>
          <filament id="1" group_id="0"/>
          <filament id="3" group_id="1"/>
        </plate></config>"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("Metadata/project_settings.config", PROJECT_TWO_EXTRUDERS)
            zf.writestr("Metadata/slice_info.config", slice_info)

        import pathlib
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "job.gcode.3mf"
            path.write_bytes(buf.getvalue())
            assert extract_slot_extruders_from_3mf(path) == [0, -1, 1]

    def test_an_unreadable_file_costs_nothing(self, tmp_path):
        """A broken file on the dispatch path must not take the print down."""
        path = tmp_path / "not-a-zip.3mf"
        path.write_bytes(b"nope")

        assert extract_slot_extruders_from_3mf(path) is None

    def test_an_absurd_slot_id_is_refused_before_the_list_is_built(self, tmp_path):
        """A file declaring filament id="50000000" would otherwise allocate a
        fifty-million-entry list, on the dispatch path."""
        slice_info = """<config><plate>
          <metadata key="index" value="1"/>
          <filament id="50000000" group_id="0"/>
        </plate></config>"""
        path = tmp_path / "hostile.gcode.3mf"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("Metadata/project_settings.config", PROJECT_TWO_EXTRUDERS)
            zf.writestr("Metadata/slice_info.config", slice_info)

        assert extract_slot_extruders_from_3mf(path) is None
