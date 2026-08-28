"""The "store on external storage" check reads the slot, not just the toggle.

Ported from upstream `fffa68ec` (#2780). The toggle says where the printer
*would* put a sent file; an empty slot says it cannot. Reading only the toggle
passed a printer that had nowhere to write, leaving the operator with archive
cards that never filled and a diagnostic insisting everything was fine.

⚠️ The interesting part is what it must NOT do. ``sdcard_state`` defaults to 0,
and 0 is also NO_SDCARD — so "the printer says there is no card" and "the
printer has not said anything yet" are the same value. Acting on that without
evidence turns every printer that has not published its storage into a fault.
"""

from __future__ import annotations

from types import SimpleNamespace

from backend.app.services.printer_diagnostic import _external_storage_check


def _state(**kw):
    base = {
        "connected": True,
        "store_to_sdcard": True,
        "sdcard_state": 1,
        "sdcard_state_seen": True,
        "print_option_support": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _printer(model="X1C"):
    return SimpleNamespace(model=model)


def _status(state, printer=None):
    return _external_storage_check(state, printer or _printer()).status


def _params(state, printer=None):
    return _external_storage_check(state, printer or _printer()).params or {}


class TestTheSlotIsPartOfTheAnswer:
    def test_toggle_on_with_a_healthy_card_passes(self):
        assert _status(_state()) == "pass"

    def test_toggle_on_with_an_empty_slot_fails(self):
        assert _status(_state(sdcard_state=0)) == "fail"

    def test_and_says_why(self):
        assert _params(_state(sdcard_state=0)).get("reason") == "no_card"


class TestSilenceIsNotEvidence:
    def test_an_unreported_slot_does_not_fail(self):
        """The default is 0, which is also NO_SDCARD. A printer that has not
        published its storage yet must not read as a printer with no card."""
        assert _status(_state(sdcard_state=0, sdcard_state_seen=False)) == "pass"

    def test_a_state_missing_the_flag_entirely_does_not_fail(self):
        bare = SimpleNamespace(connected=True, store_to_sdcard=True, print_option_support=None)

        assert _external_storage_check(bare, _printer()).status == "pass"


class TestItStillDefersWhereItShould:
    def test_a_disconnected_printer_is_skipped(self):
        assert _status(_state(connected=False, sdcard_state=0)) == "skip"

    def test_a_model_with_no_slot_is_skipped(self):
        assert _status(_state(sdcard_state=0), _printer(model="A1 mini")) == "skip"

    def test_the_toggle_being_off_is_still_its_own_failure(self):
        """Unchanged behaviour — and the branch that answers for a P1, where the
        toggle cannot be switched on at all, so the empty-slot message can never
        promise a fix that inserting a card does not deliver."""
        assert _status(_state(store_to_sdcard=False)) == "fail"
