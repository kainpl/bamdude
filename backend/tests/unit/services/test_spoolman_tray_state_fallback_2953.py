"""Spoolman charges the tray the printer said it used, not the first one loaded
(upstream 935d4b5b, #2953).

A sliced file numbers its filaments 1..N; which AMS tray each came from is
decided when the job is sent. With neither a dispatched nor a queue mapping (an
A1 publishes no mapping and drops the connection on a request-topic subscribe),
the Spoolman writer fell to the positional default and charged slot 1 to
whatever sat in the first tray — while the printer's own tray reporting named the
tray that did the work.

⚠️ Priority unchanged (CLAUDE.md, "Filament attribution believes the DISPATCHED
mapping"): the dispatched / queue mapping still wins; the printer's tray is a
rung BELOW it, and only for a print with exactly one slot carrying usage. A
multi-switch log is the tray split's (#1793). ``tray_now_at_start`` is not a rung
here — the Spoolman tracking row does not keep it, and on the printers this is
for it reads 255 anyway (print start runs before the filament loads).

Where nothing names a tray the positional default still charges, but it is a
guess: said at WARNING, and it no longer restamps the archive's colour and
material from a spool picked by position.
"""

from __future__ import annotations

import inspect
import re
from types import SimpleNamespace

from backend.app.services import spoolman_tracking
from backend.app.services.spoolman_tracking import _completion_slot_to_tray, _single_slot_tray_from_state


def _state(*, log=(), tray_now=255, last_loaded=-1, vt=()):
    return SimpleNamespace(
        tray_change_log=list(log), tray_now=tray_now, last_loaded_tray=last_loaded, raw_data={"vt_tray": list(vt)}
    )


class TestTheTrayThePrinterNamed:
    def test_a_single_logged_switch_names_the_tray(self):
        """The reporter's bundle: "Tray change during print: tray=3 at layer=0"."""
        assert _single_slot_tray_from_state(_state(log=[(3, 0)]), [(3, 0)]) == 3

    def test_more_than_one_switch_is_the_splits_not_a_single_tray(self):
        assert _single_slot_tray_from_state(_state(log=[(0, 0), (2, 80)]), [(0, 0), (2, 80)]) is None

    def test_the_current_tray_when_nothing_was_logged(self):
        assert _single_slot_tray_from_state(_state(tray_now=2), []) == 2

    def test_the_last_loaded_tray_after_the_printer_parked_it(self):
        """An A1 parks tray_now back at 255 the moment a print ends."""
        assert _single_slot_tray_from_state(_state(tray_now=255, last_loaded=1), []) == 1

    def test_255_is_a_real_external_spool_on_an_h2(self):
        assert _single_slot_tray_from_state(_state(tray_now=255, vt=[{"id": 255}]), []) == 255

    def test_nothing_named_is_none(self):
        assert _single_slot_tray_from_state(_state(), []) is None
        assert _single_slot_tray_from_state(None, []) is None


USAGE_ONE = [{"slot_id": 1, "used_g": 2.17}, {"slot_id": 2, "used_g": 0.0}]


class TestTheCompletionMapping:
    def test_a_dispatched_mapping_still_wins(self):
        mapping, source = _completion_slot_to_tray([1, 2], USAGE_ONE, [(3, 0)], _state(log=[(3, 0)]))
        assert (mapping, source) == ([1, 2], "dispatched")

    def test_a_multi_switch_log_goes_to_the_split(self):
        mapping, source = _completion_slot_to_tray(None, USAGE_ONE, [(0, 0), (2, 80)], _state())
        assert (mapping, source) == (None, "split")

    def test_the_printers_tray_maps_the_one_used_slot(self):
        mapping, source = _completion_slot_to_tray(None, USAGE_ONE, [(3, 0)], _state(log=[(3, 0)]))
        assert source == "printer-tray"
        assert spoolman_tracking._resolve_global_tray_id(1, mapping, {0: {}, 1: {}, 2: {}, 3: {}}) == 3

    def test_the_used_slot_need_not_be_the_first(self):
        usage = [{"slot_id": 1, "used_g": 0.0}, {"slot_id": 3, "used_g": 12.0}]
        mapping, source = _completion_slot_to_tray(None, usage, [], _state(tray_now=2))
        assert source == "printer-tray"
        assert spoolman_tracking._resolve_global_tray_id(3, mapping, {0: {}, 1: {}, 2: {}, 3: {}}) == 2

    def test_a_multi_colour_print_is_not_read_off_one_tray_reading(self):
        usage = [{"slot_id": 1, "used_g": 5.0}, {"slot_id": 2, "used_g": 7.0}]
        assert _completion_slot_to_tray(None, usage, [], _state(tray_now=2)) == (None, "positional")

    def test_nothing_named_is_a_positional_guess(self):
        assert _completion_slot_to_tray(None, USAGE_ONE, [], _state()) == (None, "positional")


class TestTheWriter:
    @staticmethod
    def _source() -> str:
        return re.sub(r"\s+", " ", inspect.getsource(spoolman_tracking.report_usage))

    def test_report_usage_resolves_the_mapping_through_the_helper(self):
        assert (
            "slot_to_tray, mapping_source = _completion_slot_to_tray(slot_to_tray, filament_usage, tray_changes, _state)"
            in self._source()
        )

    def test_a_guess_does_not_restamp_the_archive(self):
        source = self._source()
        guard = source.index("if not mapping_is_guess:")
        assert guard < source.index("await _apply_spool_colors_to_archive(")
        assert guard < source.index("await _apply_spool_types_to_archive(")

    def test_a_slot_that_used_nothing_claims_no_tray(self):
        """It was never charged, so the remain%-delta path stays free to cover it."""
        assert "for slot_id, used in usage_items: if used > 0: handled_global_tray_ids.add(" in self._source()
