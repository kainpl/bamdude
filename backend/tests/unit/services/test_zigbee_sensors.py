"""Freshness for sensors, which is not the rule that governs plugs.

The subsystem's existing invariant — freshness rests on a 30–45 s poll, reports
are a bonus — was written for mains-powered plugs. A battery sensor is
unreachable most of the time; polling it would time out repeatedly, hold the
shared radio lock while doing so, and flatten the cell. The deciding fact is not
the device class but RxOnWhenIdle in the node descriptor.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from backend.app.services.zigbee.sensors import PowerClass, SensorStore, power_class
from backend.tests.zigbee_fixtures import BATTERY_SENSOR_FLAGS, MAINS_DEVICE_FLAGS, node_descriptor

T0 = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)


def _node(mac_capability_flags: int):
    """A device carrying a REAL zigpy node descriptor — see zigbee_fixtures."""
    return SimpleNamespace(node_desc=node_descriptor(mac_capability_flags))


def test_a_device_that_listens_when_idle_is_mains_powered():
    assert power_class(_node(MAINS_DEVICE_FLAGS)) is PowerClass.MAINS


def test_the_real_snzb_02dr2_descriptor_reads_as_battery():
    """The regression this file exists for. Read wrongly, this device was called
    mains-powered and would have been polled every 30 s until the cell died."""
    assert power_class(_node(BATTERY_SENSOR_FLAGS)) is PowerClass.BATTERY


def test_an_unknown_node_descriptor_is_assumed_to_sleep():
    """The safe assumption: polling a sleeper wastes radio and battery, while
    not polling a mains device only costs some freshness."""
    assert power_class(SimpleNamespace()) is PowerClass.BATTERY


def test_a_descriptor_that_is_not_a_zigpy_type_is_assumed_to_sleep():
    """Anything we cannot read properly errs toward not poking the device."""
    assert power_class(SimpleNamespace(node_desc=SimpleNamespace())) is PowerClass.BATTERY


def test_a_recorded_report_becomes_a_reading():
    store = SensorStore()
    store.record("aa", "temperature", 2341, now=T0)

    reading = store.reading("aa", "temperature")
    assert reading.value == 23.41
    assert reading.unit == "°C"
    assert reading.at == T0


def test_a_sentinel_is_recorded_as_no_value_but_still_counts_as_contact():
    """The device spoke. That it had nothing to say is a different fact from it
    being unreachable."""
    store = SensorStore()
    store.record("aa", "temperature", -32768, now=T0)

    assert store.reading("aa", "temperature").value is None
    assert store.is_stale("aa", "temperature", max_interval=1800, multiplier=2, now=T0) is False


def test_a_value_older_than_its_window_is_stale():
    store = SensorStore()
    store.record("aa", "temperature", 2341, now=T0)

    fresh = T0 + timedelta(seconds=3599)
    old = T0 + timedelta(seconds=3601)
    assert store.is_stale("aa", "temperature", 1800, 2, now=fresh) is False
    assert store.is_stale("aa", "temperature", 1800, 2, now=old) is True


def test_never_reported_is_stale():
    assert SensorStore().is_stale("aa", "temperature", 1800, 2, now=T0) is True


def test_the_watchdog_fires_once_per_window_not_once_per_check():
    """One bounded attempt per window. Retrying inside the window would spend
    the radio on a device that is asleep by definition."""
    store = SensorStore()
    store.record("aa", "temperature", 2341, now=T0)
    later = T0 + timedelta(seconds=3601)

    assert store.due_for_watchdog("aa", "temperature", window=3600, now=later) is True
    store.note_attempt("aa", now=later)
    assert store.due_for_watchdog("aa", "temperature", window=3600, now=later + timedelta(seconds=60)) is False
    assert store.due_for_watchdog("aa", "temperature", window=3600, now=later + timedelta(seconds=3601)) is True


def test_three_empty_windows_make_a_sensor_unreachable():
    """One failed read proves nothing: a healthy sleeper can miss the ~7.7 s in
    which its parent holds the request."""
    store = SensorStore()
    for _ in range(2):
        store.note_attempt("aa", now=T0)
    assert store.empty_windows("aa") == 2
    assert store.is_unreachable("aa") is False

    store.note_attempt("aa", now=T0)
    assert store.empty_windows("aa") == 3
    assert store.is_unreachable("aa") is True


def test_a_report_clears_the_empty_window_count():
    store = SensorStore()
    store.note_attempt("aa", now=T0)
    store.note_attempt("aa", now=T0)

    store.record("aa", "temperature", 2341, now=T0)

    assert store.empty_windows("aa") == 0


def test_forgetting_a_sensor_drops_everything_about_it():
    store = SensorStore()
    store.record("aa", "temperature", 2341, now=T0)

    store.forget("aa")

    assert store.reading("aa", "temperature") is None
    assert store.known_ieees() == ()


def test_addresses_are_matched_case_insensitively():
    """zigpy stringifies EUI64 lower-case; a route echoes whatever was typed."""
    store = SensorStore()
    store.record("AA:BB", "temperature", 2341, now=T0)

    assert store.reading("aa:bb", "temperature").value == 23.41


def test_an_unknown_measurement_key_is_ignored():
    """Devices report far more than we asked for; a key outside the registry is
    noise, not an error."""
    store = SensorStore()
    store.record("aa", "radiation", 42, now=T0)

    assert store.known_ieees() == ()
