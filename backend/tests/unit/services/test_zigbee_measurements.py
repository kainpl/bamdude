"""Every conversion here is a silent trap if it is wrong: the number keeps its
plausible shape and only the meaning changes. Verified against the attribute
types in the installed zigpy."""

import pytest

from backend.app.services.zigbee.measurements import (
    BY_KEY,
    measurement_keys_for,
    to_display,
    to_raw_change,
)


def test_temperature_is_hundredths_of_a_degree():
    assert to_display(BY_KEY["temperature"], 2341) == pytest.approx(23.41)


def test_humidity_is_hundredths_of_a_percent():
    assert to_display(BY_KEY["humidity"], 4120) == pytest.approx(41.2)


def test_battery_is_half_percent_units():
    """200 is a full battery, not 200 %. Getting this wrong halves or doubles
    every battery reading and still looks like a percentage."""
    assert to_display(BY_KEY["battery"], 200) == pytest.approx(100.0)
    assert to_display(BY_KEY["battery"], 74) == pytest.approx(37.0)


def test_battery_voltage_is_tenths_of_a_volt():
    assert to_display(BY_KEY["battery_voltage"], 30) == pytest.approx(3.0)


def test_co2_is_a_fraction_not_ppm():
    """ZCL 0x040D carries a float fraction: 0.0004 is 400 ppm."""
    assert to_display(BY_KEY["co2"], 0.0004) == pytest.approx(400.0)


def test_pm25_is_micrograms_as_reported():
    assert to_display(BY_KEY["pm25"], 12.5) == pytest.approx(12.5)


def test_sentinels_are_not_measurements():
    assert to_display(BY_KEY["temperature"], -32768) is None
    assert to_display(BY_KEY["humidity"], 0xFFFF) is None
    assert to_display(BY_KEY["battery"], 0xFF) is None
    assert to_display(BY_KEY["co2"], float("nan")) is None


def test_implausible_values_are_refused():
    """A number outside physical reality is not shown at all — the same rule
    that governs plug power, for the same reason."""
    assert to_display(BY_KEY["temperature"], 900000) is None
    assert to_display(BY_KEY["humidity"], 30000) is None  # 300 %


def test_reportable_change_converts_display_units_to_raw():
    """0.5 °C is 50 hundredths; 1 % of battery is 2 half-percent steps."""
    assert to_raw_change(BY_KEY["temperature"], 0.5) == 50
    assert to_raw_change(BY_KEY["battery"], 1.0) == 2


def test_cluster_ids_map_to_keys():
    assert set(measurement_keys_for({0x0402, 0x0405, 0x0006})) == {"temperature", "humidity"}
    assert measurement_keys_for({0x0006}) == ()


def test_a_battery_alone_does_not_make_a_sensor():
    """Plenty of devices carry a Power Configuration cluster; nobody pairs one
    to watch its battery."""
    assert measurement_keys_for({0x0001}) == ()


def test_every_registry_entry_round_trips_its_own_change():
    """Guards the next row somebody adds: a scale that cannot express its own
    default change is a row that silently reports on every flicker."""
    for m in BY_KEY.values():
        assert to_raw_change(m, m.default_reportable_change) >= 1
