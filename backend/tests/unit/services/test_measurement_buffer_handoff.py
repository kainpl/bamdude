"""The buffer's hand-off to the database and what a reading carries (spec plug-meter-watchdog W3, W10).

A reader copies the buffer first and reads the database second; for that to never lose a reading, a
batch the flush has taken stays visible until its commit is acknowledged, a known rollback puts it
back, and a commit of unknown outcome leaves each moment as an unusable marker.
"""

import threading

import pytest

from backend.app.services.measurement_buffer import MAX_BUFFERED, MeasurementBuffer


@pytest.fixture
def buf():
    return MeasurementBuffer()


def test_a_known_off_is_zero_watts_whatever_the_firmware_repeats(buf):
    buf.record_power(1, 33.0, source_token="t", state="OFF", phantom_risk=True)
    (sample,), _ = buf.peek_power(1)
    assert (sample.power, sample.reason, sample.source_token) == (0.0, None, "t")


def test_an_unknown_state_makes_a_risky_plugs_reading_unusable(buf):
    buf.record_power(1, 33.0, source_token="t", state=None, phantom_risk=True)
    (sample,), _ = buf.peek_power(1)
    assert (sample.power, sample.reason) == (33.0, "state_unknown")


def test_a_plug_without_the_risk_keeps_its_watts_without_a_state(buf):
    """O14: HTTP Status 8 and MQTT without a relay topic report no state, and must still count."""
    buf.record_power(1, 200.0, source_token="t", state=None, phantom_risk=False)
    (sample,), _ = buf.peek_power(1)
    assert (sample.power, sample.reason) == (200.0, None)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0, True, "x"])
def test_a_value_that_is_no_reading_is_kept_as_unusable(buf, bad):
    buf.record_power(1, bad, source_token="t")
    (sample,), _ = buf.peek_power(1)
    assert (sample.power, sample.reason) == (0.0, "invalid_input")


def test_a_taken_batch_stays_visible_until_acknowledged(buf):
    buf.record_power(1, 100.0, source_token="t")
    taken = buf.take_power()
    assert len(taken) == 1
    assert len(buf.peek_power(1)[0]) == 1  # in flight, still seen
    buf.ack_power()
    assert buf.peek_power(1)[0] == []


def test_a_known_rollback_puts_the_batch_back_before_newer_readings(buf):
    buf.record_power(1, 100.0, source_token="t")
    buf.take_power()
    buf.record_power(1, 150.0, source_token="t")
    buf.return_power()
    assert [s.power for s in buf.take_power()] == [100.0, 150.0]


def test_an_unknown_commit_leaves_every_moment_as_a_marker(buf):
    for watts in (100.0, 110.0, 120.0):
        buf.record_power(1, watts, source_token="t")
    taken = buf.take_power()
    buf.mark_unknown()
    pending, _ = buf.peek_power(1)
    assert [(s.recorded_at, s.reason) for s in pending] == [(s.recorded_at, "batch_unknown") for s in taken]


def test_a_second_take_before_the_answer_takes_nothing(buf):
    buf.record_power(1, 100.0, source_token="t")
    buf.take_power()
    buf.record_power(1, 110.0, source_token="t")
    assert buf.take_power() == []


def test_overflow_keeps_where_the_lost_readings_were(buf):
    for i in range(MAX_BUFFERED + 10):
        buf.record_power(1, float(i), source_token="t")
    pending, _ = buf.peek_power(1)
    lost = sorted(s.recorded_at for s in pending if s.reason == "buffer_loss")
    kept = [s for s in pending if s.reason is None]
    assert 1 <= len(lost) <= 2 and len(kept) == MAX_BUFFERED  # one when the run shares a microsecond
    assert lost[-1] <= kept[0].recorded_at


def test_a_retired_buffer_keeps_nothing_and_says_so(buf):
    buf.record_power(1, 100.0, source_token="t")
    buf.retire()
    buf.record_power(1, 110.0, source_token="t")
    assert buf.peek_power(1) == ([], True)
    assert buf.take_power() == []


def test_readings_from_another_thread_during_a_hand_off_are_never_lost(buf):
    """MQTT readings arrive on paho's thread while the loop takes and acknowledges."""
    done = threading.Event()

    def produce():
        for i in range(5000):
            buf.record_power(1, float(i), source_token="t")
        done.set()

    seen = 0
    worker = threading.Thread(target=produce)
    worker.start()
    while not done.is_set() or buf.pending():
        seen += len(buf.take_power())
        buf.ack_power()
    worker.join()
    seen += len(buf.take_power())
    assert seen == 5000


def test_unknown_commit_markers_survive_overflow(monkeypatch):
    """MW-R3: a batch whose commit may have landed keeps every moment unusable, whatever arrives after it.

    Folding its markers into the two edges of a loss span would leave the DB copies between them
    usable again; newer readings give way instead.
    """
    from datetime import datetime, timedelta, timezone

    from backend.app.services import measurement_buffer as bm

    monkeypatch.setattr(bm, "MAX_BUFFERED", 101)
    buffer = bm.MeasurementBuffer()
    start = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
    for i in range(101):
        monkeypatch.setattr(bm, "_now", lambda at=start + timedelta(minutes=i): at)
        buffer.record_power(1, 200.0, source_token="t")
    buffer.take_power()
    for i in range(101, 202):
        monkeypatch.setattr(bm, "_now", lambda at=start + timedelta(minutes=i): at)
        buffer.record_power(1, 200.0, source_token="t")
    buffer.mark_unknown()

    samples, _ = buffer.peek_power(1)
    unknown = sorted(s.recorded_at for s in samples if s.reason == bm.BATCH_UNKNOWN)
    assert unknown == [start + timedelta(minutes=i) for i in range(101)]
    # Memory stays bounded: the newer readings that did not fit are a loss span, not kept.
    assert len([s for s in samples if s.reason is None]) <= 2
