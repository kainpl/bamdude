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
    """Guards the next row somebody adds.

    A change that converts to zero asks the device to report on every sample; a
    row whose scale cannot express its own default is broken in one direction or
    the other. Integer attributes additionally floor at 1, since anything below
    that IS zero to the device — but a float attribute must not be forced up to
    1, which for CO2 would mean a million ppm.
    """
    for m in BY_KEY.values():
        raw = to_raw_change(m, m.default_reportable_change)
        assert raw > 0, m.key
        if m.raw_is_integer:
            assert isinstance(raw, int) and raw >= 1, m.key


class TestFloatValuedMeasurementsKeepTheirPrecision:
    """CO2 and PM2.5 are ZCL floats, and rounding their change to an integer
    destroys the setting silently.

    A CO2 change of 1 ppm is 0.000001 in raw units: rounded to an int it becomes
    0, and a floor of 1 turns it into "tell me when it moves by a million ppm" —
    a device that then never reports, with nothing anywhere saying why.
    """

    def test_one_ppm_of_co2_survives_the_conversion(self):
        assert to_raw_change(BY_KEY["co2"], 1.0) == pytest.approx(0.000001)

    def test_a_tenth_of_a_microgram_survives(self):
        assert to_raw_change(BY_KEY["pm25"], 0.1) == pytest.approx(0.1)

    def test_integer_attributes_still_round_and_never_reach_zero(self):
        """The floor stays where it belongs: a change of 0 on an integer
        attribute asks for a report on every sample."""
        assert to_raw_change(BY_KEY["temperature"], 0.1) == 10
        assert to_raw_change(BY_KEY["humidity"], 0.1) == 10
        assert to_raw_change(BY_KEY["battery"], 0.5) == 1
        assert to_raw_change(BY_KEY["temperature"], 0.001) == 1
        assert isinstance(to_raw_change(BY_KEY["temperature"], 0.1), int)


def test_the_defaults_are_the_agreed_ones():
    """Pinned deliberately: these were chosen against ZHA's own numbers, and a
    silent drift in a default is a farm-wide change nobody reviews."""
    expected = {
        "temperature": (30, 900, 0.1),
        "humidity": (30, 900, 0.1),
        "co2": (30, 900, 1.0),
        "pm25": (30, 900, 0.1),
        "battery": (3600, 10800, 0.5),
        "battery_voltage": (3600, 10800, 0.1),
    }
    for key, (minimum, maximum, change) in expected.items():
        m = BY_KEY[key]
        assert (m.default_min_interval, m.default_max_interval, m.default_reportable_change) == (
            minimum,
            maximum,
            change,
        ), key
