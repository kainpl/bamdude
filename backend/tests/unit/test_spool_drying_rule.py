"""When a finished AMS drying cycle counts as having dried its spools (upstream 84f320b85).

At least half of the cycle's length, or an end with no more than five minutes
left. The length is what was asked for when BamDude started the cycle, else the
highest countdown it saw.
"""

from __future__ import annotations

from backend.app.services.bambu_mqtt import DryingCycleEnd
from backend.app.services.spool_drying import DryingRecord, drying_record


def _cycle(remaining, peak, *, start_seen=True, temp=None, hours=None) -> DryingCycleEnd:
    return DryingCycleEnd(
        ams_id=0,
        remaining_minutes=remaining,
        peak_minutes=peak,
        start_seen=start_seen,
        target_temp=temp,
        target_hours=hours,
    )


def test_a_cycle_that_ran_to_term_counts():
    assert drying_record(_cycle(1, 480, temp=55, hours=8)) == DryingRecord(temp=55, hours=8.0)


def test_five_minutes_left_is_still_to_term():
    assert drying_record(_cycle(5, 480)) is not None


def test_stopped_at_forty_percent_does_not_count():
    assert drying_record(_cycle(288, 480, temp=55, hours=8)) is None


def test_stopped_at_sixty_percent_counts_with_the_hours_it_ran():
    assert drying_record(_cycle(192, 480, temp=55, hours=8)) == DryingRecord(temp=55, hours=4.8)


def test_exactly_half_counts():
    assert drying_record(_cycle(240, 480)) == DryingRecord(temp=None, hours=4.0)


def test_the_requested_length_outranks_a_peak_seen_late():
    """BamDude asked for 8 h but first saw the countdown at 300 min; stopped at
    250 the cycle ran 230 of 480 minutes, not 50 of 300."""
    assert drying_record(_cycle(250, 300, start_seen=False, temp=55, hours=8)) is None
    assert drying_record(_cycle(200, 300, start_seen=False, temp=55, hours=8)) == DryingRecord(temp=55, hours=4.7)


def test_a_cycle_met_half_way_with_no_target_is_dated_but_its_hours_are_unknown():
    assert drying_record(_cycle(2, 200, start_seen=False)) == DryingRecord(temp=None, hours=None)


def test_a_cycle_started_at_the_printer_has_no_temperature():
    assert drying_record(_cycle(0, 240)) == DryingRecord(temp=None, hours=4.0)


def test_a_parked_timer_cleared_by_stop_dried_nothing():
    assert drying_record(_cycle(720, 720, temp=65, hours=12)) is None


def test_a_start_the_printer_did_not_apply_dries_nothing():
    """A longer cycle asked for over a timer the printer keeps (and does not run): the countdown that was
    watched is the length, not the request — or a stop would read as two thirds of a cycle that never heated."""
    assert drying_record(_cycle(240, 240, start_seen=True, temp=55, hours=12)) is None


def test_a_refused_restart_does_not_lengthen_the_cycle_that_ran():
    """Parked at 300 of 480, a 12-hour restart refused, then stopped: 37 % of the real cycle, not 58 %."""
    assert drying_record(_cycle(300, 480, start_seen=True, temp=55, hours=12)) is None
