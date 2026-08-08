"""Per-VP opt-in for the slicer's own AMS pick (#2700).

Two spools of the same red PLA sit in different slots. The file cannot tell them
apart — its per-slot type and colour are identical — so a mapping derived from
those cannot honour the slot the user chose in Bambu Studio. The slicer already
resolved it; this keeps that answer instead of re-deriving a worse one.

It is a **toggle**, not a fix, and the reason is the whole point of these tests:
a queue item that carries a mapping makes ``_ensure_ams_mapping`` return early,
so ``_compute_ams_mapping_for_printer`` never runs — and that function holds
``prefer_lowest_filament``, the AMS-Filament-Backup gate (#1766), the
inventory-remain overrides (#1508) and the FTS routing rule (#2186). FTS has no
upstream counterpart and is the one that can strand a print on a wrong-nozzle
slot rather than merely pick a different spool.

**Off is exactly today's behaviour**, and that is what most of this file pins.
"""

from __future__ import annotations

import inspect
import json

from backend.app.services import virtual_printer as vp_pkg
from backend.app.services.print_scheduler import PrintScheduler


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


class TestWhatTheToggleSwitchesOff:
    """These live in ``_compute_ams_mapping_for_printer``, which a stored
    mapping skips. The UI description has to name them, so the test names them
    too — if one is ever moved out of that function, this is the reminder that
    the description became a lie."""

    def test_the_skipped_function_still_owns_all_four(self) -> None:
        source = inspect.getsource(PrintScheduler._compute_ams_mapping_for_printer)

        assert "prefer_lowest_filament" in source
        assert "ams_auto_switch_filament" in source, "the AMS-Backup gate (#1766)"
        assert "_build_inventory_remain_overrides" in source, "inventory remain (#1508)"
        assert "fila_switch" in source, "FTS routing (#2186) — ours, not upstream's"

    def test_a_resolved_mapping_makes_the_computation_be_skipped(self) -> None:
        """States the mechanism the toggle trades against, in one place."""
        source = inspect.getsource(PrintScheduler._ensure_ams_mapping)

        assert "if item.ams_mapping and not _mapping_is_all_unresolved(stored_mapping):" in source
        assert "return" in source


class TestTheStoredShape:
    def test_it_is_a_json_array_of_slots(self) -> None:
        """Same shape ``_ensure_ams_mapping`` already reads, so nothing
        downstream has to learn a second encoding."""
        assert json.loads("[0, -1, 2]") == [0, -1, 2]
