"""Per-VP opt-in for the slicer's own AMS pick (#2700).

Two spools of the same red PLA sit in different slots. The file cannot tell them
apart — its per-slot type and colour are identical — so a mapping derived from
those cannot honour the slot the user chose in Bambu Studio. The slicer already
resolved it; this keeps that answer instead of re-deriving a worse one.

It is a **toggle**, not a fix, and the reason is the whole point of these tests:
the captured array reaches ``prepare_routing`` as ``manual_mapping: True``,
which is what turns a mapping into PHYSICAL PINS — a person pointing at trays.
A pinned slot is bound to the feed the slicer named; it is not routed at
dispatch, so prefer_lowest_filament, the AMS-Filament-Backup gate (#1766), the
inventory-remain overrides (#1508) and the FTS routing rule (#2186) have nothing
left to decide for it.

**Off is exactly today's behaviour**, and that is what most of this file pins.
"""

from __future__ import annotations

import inspect
import json

from backend.app.services import virtual_printer as vp_pkg
from backend.app.services.filament_policy import choices_policy


class TestTheDefaultIsUnchangedBehaviour:
    def test_the_column_defaults_to_off(self) -> None:
        from backend.app.models.virtual_printer import VirtualPrinter

        assert VirtualPrinter.__table__.c.save_ams_mapping.default.arg is False

    def test_the_migration_adds_it_as_a_boolean_default_false(self) -> None:
        """``BOOLEAN DEFAULT 0`` — the helper translates the literal for
        PostgreSQL, which rejects an integer default on a boolean column."""
        from backend.app.migrations import m131_vp_save_ams_mapping as m

        assert m.version == 131
        source = inspect.getsource(m.upgrade)
        assert "virtual_printers" in source
        assert "BOOLEAN DEFAULT 0" in source


class TestTheCaptureIsGated:
    """The capture reads ``slicer_opts['ams_mapping']`` and writes it onto the
    queue item — but only when the VP asked for it."""

    def _source(self) -> str:
        return inspect.getsource(vp_pkg.manager)

    def test_the_toggle_guards_the_capture(self) -> None:
        source = self._source()
        assert "if self.save_ams_mapping:" in source
        assert "ams_mapping_json = _captured_ams_mapping(" in source

    def test_an_unresolved_mapping_is_never_stored(self) -> None:
        """All-[-1] is the slicer saying "I could not resolve this", not a pick.
        Stored, it would be read downstream as an explicit external-spool
        selection and print against an empty feed — the #2589 failure."""
        capture = vp_pkg.manager._captured_ams_mapping

        assert capture({"ams_mapping": [-1, -1]}, dual_nozzle=False, vp_name="t") is None
        unmapped = [{"ams_id": 255, "slot_id": 255}] * 2
        assert capture({"ams_mapping": [-1, -1], "ams_mapping2": unmapped}, dual_nozzle=True, vp_name="t") is None
        assert capture(None, dual_nozzle=False, vp_name="t") is None
        assert capture({"ams_mapping": "not json"}, dual_nozzle=False, vp_name="t") is None


class TestTheExternalSpoolIsKept:
    """The slicer writes the external spool as ``-1`` in the flat list and names it only in
    ``ams_mapping2`` (upstream Bambuddy #3237). Read flat, that slot had no pin and the job was held
    for a mapping review; a job fed only from the external spool was not captured at all."""

    capture = staticmethod(vp_pkg.manager._captured_ams_mapping)

    def test_one_filament_from_the_external_spool_keeps_its_pick(self) -> None:
        opts = {
            "ams_mapping": [-1, 3, 2],
            "ams_mapping2": [{"ams_id": 255, "slot_id": 0}, {"ams_id": 0, "slot_id": 3}, {"ams_id": 0, "slot_id": 2}],
        }

        stored = self.capture(opts, dual_nozzle=False, vp_name="t")

        assert json.loads(stored) == [254, 3, 2]
        pins = choices_policy({"ams_mapping": stored, "use_ams": True, "manual_mapping": True}).physical_pins
        assert sorted(pins) == [1, 2, 3], "every slot the plate uses is pinned, the external one included"

    def test_a_job_fed_only_from_the_external_spool_is_a_pick(self) -> None:
        opts = {"ams_mapping": [-1], "ams_mapping2": [{"ams_id": 255, "slot_id": 0}]}

        assert json.loads(self.capture(opts, dual_nozzle=False, vp_name="t")) == [254]

    def test_a_two_holder_printer_keeps_which_holder(self) -> None:
        opts = {
            "ams_mapping": [-1, -1],
            "ams_mapping2": [{"ams_id": 254, "slot_id": 0}, {"ams_id": 255, "slot_id": 0}],
        }

        assert json.loads(self.capture(opts, dual_nozzle=True, vp_name="t")) == [254, 255]

    def test_a_mapping_without_the_detail_is_stored_as_sent(self) -> None:
        assert json.loads(self.capture({"ams_mapping": [0, -1, 2]}, dual_nozzle=False, vp_name="t")) == [0, -1, 2]

    def test_the_queue_item_carries_it(self) -> None:
        assert "ams_mapping=ams_mapping_json," in inspect.getsource(vp_pkg.manager)


class TestTheCapturedMappingIsConsumedAsAPin:
    """The live path, end of story: ``manual_mapping: True`` or it is only a plan."""

    def test_the_manager_hands_it_over_as_a_manual_mapping(self) -> None:
        source = inspect.getsource(vp_pkg.manager)

        assert '"ams_mapping": ams_mapping_json,' in source
        assert '"manual_mapping": ams_mapping_json is not None,' in source

    def test_that_flag_is_what_makes_the_slots_physical_pins(self) -> None:
        """Same array, both answers — the flag is the whole difference."""
        mapping = json.dumps([0, -1, 2])
        pinned = choices_policy({"ams_mapping": mapping, "use_ams": True, "manual_mapping": True})
        plan_only = choices_policy({"ams_mapping": mapping, "use_ams": True})

        assert pinned.mode == "pinned"
        assert sorted(pinned.physical_pins) == [1, 3], "slot 2 is unresolved (-1) and is not a pin"
        assert pinned.physical_pins[1]["source_id"] == 0
        assert (plan_only.mode, plan_only.physical_pins) == ("auto", {})


class TestTheStoredShape:
    def test_it_is_a_json_array_of_slots(self) -> None:
        """``choices_policy`` decodes exactly this, so nothing downstream has to
        learn a second encoding."""
        assert json.loads("[0, -1, 2]") == [0, -1, 2]
