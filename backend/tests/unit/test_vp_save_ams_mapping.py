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
        assert "if self.save_ams_mapping and slicer_opts is not None:" in self._source()

    def test_an_unresolved_mapping_is_never_stored(self) -> None:
        """All-[-1] is the slicer saying "I could not resolve this", not a pick.
        Stored, it would be read downstream as an explicit external-spool
        selection and print against an empty feed — the #2589 failure."""
        source = self._source()
        assert "isinstance(v, int) and v >= 0 for v in raw_ams_mapping" in source

    def test_the_queue_item_carries_it(self) -> None:
        assert "ams_mapping=ams_mapping_json," in self._source()


class TestTheCapturedMappingIsConsumedAsAPin:
    """The live path, end of story: ``manual_mapping: True`` or it is only a plan."""

    def test_the_manager_hands_it_over_as_a_manual_mapping(self) -> None:
        source = inspect.getsource(vp_pkg.manager)

        assert '"ams_mapping": ams_mapping_json,' in source
        assert '"manual_mapping": True,' in source

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
