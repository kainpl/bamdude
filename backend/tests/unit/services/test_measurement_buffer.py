"""What sits between a device callback and the database.

Report handlers are synchronous and must not block or raise — zigpy logs a
listener exception at debug level and moves on, so a failed write there would
simply vanish. Appending to memory cannot fail; the loop does the I/O.
"""

import pytest


@pytest.fixture(autouse=True)
def empty_buffer():
    from backend.app.services.measurement_buffer import measurement_buffer

    measurement_buffer.drain()
    yield
    measurement_buffer.drain()


def test_a_power_reading_is_kept_until_drained():
    from backend.app.services.measurement_buffer import measurement_buffer

    measurement_buffer.record_power(1, 42.5)
    power, sensors = measurement_buffer.drain()

    assert [(s.plug_id, s.power) for s in power] == [(1, 42.5)]
    assert sensors == []


def test_draining_empties_it():
    """Otherwise the next flush writes everything a second time."""
    from backend.app.services.measurement_buffer import measurement_buffer

    measurement_buffer.record_power(1, 42.5)
    measurement_buffer.drain()

    assert measurement_buffer.drain() == ([], [])


def test_every_reading_is_kept_even_when_identical():
    """No de-duplication, deliberately: skipping repeats would make "no row"
    mean both "unchanged" and "we had no reading", and a flat line and a gap
    would look the same."""
    from backend.app.services.measurement_buffer import measurement_buffer

    for _ in range(3):
        measurement_buffer.record_power(1, 0.0)

    power, _ = measurement_buffer.drain()

    assert len(power) == 3


def test_a_reading_with_no_value_is_not_buffered():
    """No value, no row — never a fabricated zero. A zero is differenced and
    believed downstream; a missing row is honest."""
    from backend.app.services.measurement_buffer import measurement_buffer

    measurement_buffer.record_power(1, None)
    measurement_buffer.record_sensor("aa:bb", "temperature", None)

    assert measurement_buffer.drain() == ([], [])


def test_a_sensor_reading_carries_its_kind():
    from backend.app.services.measurement_buffer import measurement_buffer

    measurement_buffer.record_sensor("aa:bb", "humidity", 41.2)
    _power, sensors = measurement_buffer.drain()

    assert [(s.ieee, s.kind, s.value) for s in sensors] == [("aa:bb", "humidity", 41.2)]


def test_recording_never_raises_whatever_it_is_given():
    """This runs inside a zigpy callback. A raise there is swallowed at debug
    level, so it would not even be visible — it would just stop the report."""
    from backend.app.services.measurement_buffer import measurement_buffer

    measurement_buffer.record_power(1, "not a number")
    measurement_buffer.record_sensor("aa:bb", "temperature", object())

    assert measurement_buffer.drain() == ([], [])


def test_it_does_not_grow_without_bound():
    """The loop that drains this can stop — a database outage, a cancelled
    task. Memory must not be the thing that fails next."""
    from backend.app.services.measurement_buffer import MAX_BUFFERED, measurement_buffer

    for i in range(MAX_BUFFERED + 500):
        measurement_buffer.record_power(1, float(i))

    power, _ = measurement_buffer.drain()

    assert len(power) == MAX_BUFFERED
    assert power[-1].power == float(MAX_BUFFERED + 499), "the newest readings are the ones kept"


def test_pending_reports_what_is_waiting():
    from backend.app.services.measurement_buffer import measurement_buffer

    measurement_buffer.record_power(1, 1.0)
    measurement_buffer.record_sensor("aa:bb", "temperature", 20.0)

    assert measurement_buffer.pending() == 2
