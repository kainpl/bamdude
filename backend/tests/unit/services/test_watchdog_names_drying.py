"""When a print never starts, say the AMS was drying.

Ported from upstream #2758. Dispatching to an X2D with two AMS units mid-drying
failed silently: the file uploaded, the printer accepted it and stayed idle. The
watchdog waits for an active state, and a drying refusal is not one — so it
timed out, re-uploaded the whole 3MF twice more, and closed with advice about
the printer's screen and its SD card. The slicer, asked directly, said it could
not start the job because of the drying.

⚠️ **Detection only, no gate.** This hardware supports drying *through* a print,
so stopping every cycle before dispatch would tear down cycles the machine is
happy to run. One of the two units was also drying without its external PSU,
which would make it a power-budget problem at start-of-print calibration rather
than a drying one — so the message names both possibilities instead of
asserting one.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import backend.app.models.printer_location  # noqa: F401
from backend.app.services.print_scheduler import _drying_ams_ids


def _status(*units: dict) -> SimpleNamespace:
    return SimpleNamespace(raw_data={"ams": list(units)})


class TestReadingTheTelemetry:
    def test_a_drying_unit_is_named(self):
        assert _drying_ams_ids(_status({"id": 0, "dry_time": 700})) == [0]

    def test_every_drying_unit_is_named(self):
        """The reported case had two."""
        assert _drying_ams_ids(_status({"id": 0, "dry_time": 700}, {"id": 1, "dry_time": 120})) == [0, 1]

    def test_an_idle_unit_is_not(self):
        assert _drying_ams_ids(_status({"id": 0, "dry_time": 0}, {"id": 1, "dry_time": 700})) == [1]

    def test_a_printer_with_no_ams_says_nothing(self):
        assert _drying_ams_ids(_status()) == []

    def test_a_status_with_no_raw_data_does_not_raise(self):
        """The watchdog polls a live object; a half-populated one must not turn
        a diagnostic into a crash on the dispatch path."""
        assert _drying_ams_ids(SimpleNamespace()) == []
        assert _drying_ams_ids(SimpleNamespace(raw_data=None)) == []

    def test_junk_in_the_payload_is_skipped_not_fatal(self):
        assert _drying_ams_ids(_status({"id": 0, "dry_time": "soon"}, {"id": 1, "dry_time": 5})) == [1]
        assert _drying_ams_ids(SimpleNamespace(raw_data={"ams": ["nonsense", {"id": 2, "dry_time": 5}]})) == [2]


class TestTheMessage:
    """Asserted on the source, not by driving the watchdog.

    ⚠️ The give-up branch needs a queue row, a DB session, three failed dispatch
    attempts and a 90-second window; the thing worth pinning is far smaller —
    that the drying case has its own message and that it names both possible
    obstacles rather than asserting one.
    """

    @staticmethod
    def _source() -> str:
        import inspect

        from backend.app.services.print_scheduler import PrintScheduler

        return inspect.getsource(PrintScheduler)

    def test_the_drying_case_gets_its_own_message(self):
        source = self._source()
        assert "if drying_ams_ids:" in source
        assert "drying throughout" in source

    def test_it_names_the_power_supply_too(self):
        """⚠️ Not asserted as the cause. One of the two reported units was
        drying without its external PSU, which would make this a power budget
        problem at start-of-print calibration rather than a drying one."""
        assert "external power supply" in self._source()

    def test_the_generic_message_survives_for_every_other_case(self):
        assert "confirm its SD card is readable" in self._source()

    def test_the_reading_is_latched_rather_than_taken_at_the_end(self):
        """⚠️ Drying can finish, or be stopped by the user, part-way through the
        window. What matters is the state the printer was in when it declined to
        start — so the first sighting wins and later polls cannot erase it."""
        assert "drying_ams_ids = drying_ams_ids or _drying_ams_ids(status)" in self._source()


class TestNothingIsStopped:
    def test_the_diagnostic_never_sends_a_stop(self):
        """⚠️ The whole design decision. If this ever grows a
        send_drying_command call, drying that the hardware was happy to continue
        is being torn down on a guess."""
        import inspect

        source = inspect.getsource(_drying_ams_ids)
        assert "send_drying_command" not in source
        assert "_stop_drying" not in source


@pytest.mark.parametrize("dry_time", [1, 720, 99999])
def test_any_positive_remaining_time_counts(dry_time):
    assert _drying_ams_ids(_status({"id": 0, "dry_time": dry_time})) == [0]
