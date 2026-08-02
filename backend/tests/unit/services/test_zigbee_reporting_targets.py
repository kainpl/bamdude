"""One vocabulary for both device classes.

The registry these project from does not change: ``measurements.py`` keeps
answering its own question (what a sensor measures and how to read it) and keeps
its pinning test. This module only lifts out the third thing it happened to
carry — how reporting is configured — because that is the part plugs share.
"""

import pytest

from backend.app.services.zigbee.devices import (
    ELECTRICAL_MEASUREMENT,
    METERING,
    ON_OFF,
    DeviceInfo,
    DeviceKind,
)
from backend.app.services.zigbee.reporting_targets import targets_for


def _info(kind, measurements=(), has_em=True, has_metering=True, extra_clusters=()):
    """``measurements`` is the CLASSIFYING list; ``cluster_ids`` is what the
    device actually carries. They differ, and the difference is load-bearing —
    see the battery test below."""
    from backend.app.services.zigbee.measurements import BY_KEY

    clusters = {BY_KEY[key].cluster for key in measurements if key in BY_KEY} | set(extra_clusters)
    if kind is DeviceKind.PLUG:
        clusters |= {ON_OFF}
        if has_em:
            clusters.add(ELECTRICAL_MEASUREMENT)
        if has_metering:
            clusters.add(METERING)
    return DeviceInfo(
        ieee="aa:bb",
        nwk=1,
        manufacturer="X",
        model="Y",
        kind=kind,
        measurements=measurements,
        cluster_ids=frozenset(clusters),
        has_metering=has_metering,
        has_electrical_measurement=has_em,
        reject_reason=None,
    )


def test_a_sensor_gets_one_target_per_measurement():
    targets = targets_for(_info(DeviceKind.SENSOR, ("temperature", "humidity")))

    assert {t.key for t in targets} == {"temperature", "humidity"}


def test_sensor_defaults_come_from_the_measurement_registry():
    """Projected, not copied. Changing a default in measurements.py must move
    this without anyone editing two places."""
    from backend.app.services.zigbee.measurements import BY_KEY

    target = next(iter(targets_for(_info(DeviceKind.SENSOR, ("temperature",)))))
    m = BY_KEY["temperature"]

    assert (target.min_interval, target.max_interval, target.reportable_change) == (
        m.default_min_interval,
        m.default_max_interval,
        m.default_reportable_change,
    )
    assert (target.cluster, target.attribute) == (m.cluster, m.attribute)


def test_a_sensor_attribute_stays_the_name_the_registry_uses():
    """zigpy accepts either a name or an id, and the registry speaks names.
    Converting here would move the resolution away from the cluster that owns
    it — and the cluster is the only thing that can do it correctly."""
    target = next(iter(targets_for(_info(DeviceKind.SENSOR, ("temperature",)))))

    assert target.attribute == "measured_value"


def test_a_plug_gets_state_power_and_energy():
    targets = targets_for(_info(DeviceKind.PLUG))

    assert {t.key for t in targets} == {"state", "power", "energy"}


def test_plug_bounds_are_the_ones_zha_uses():
    by_key = {t.key: t for t in targets_for(_info(DeviceKind.PLUG))}

    assert (by_key["state"].min_interval, by_key["state"].max_interval) == (0, 900)
    assert (by_key["energy"].min_interval, by_key["energy"].max_interval) == (30, 900)
    assert (by_key["power"].min_interval, by_key["power"].max_interval) == (5, 900)


def test_plug_targets_point_at_the_clusters_that_carry_them():
    by_key = {t.key: t for t in targets_for(_info(DeviceKind.PLUG))}

    assert by_key["state"].cluster == ON_OFF
    assert by_key["energy"].cluster == METERING
    assert by_key["power"].cluster == ELECTRICAL_MEASUREMENT


def test_only_max_interval_is_editable_on_a_relay():
    """reportable_change is meaningless for a relay, and a min_interval above
    zero can only delay the confirmation of a command we sent ourselves."""
    state = next(t for t in targets_for(_info(DeviceKind.PLUG)) if t.key == "state")

    assert state.editable == ("max_interval",)


def test_every_other_target_is_fully_editable():
    for info in (_info(DeviceKind.SENSOR, ("temperature",)), _info(DeviceKind.PLUG)):
        for target in targets_for(info):
            if target.key == "state":
                continue
            assert target.editable == ("min_interval", "max_interval", "reportable_change"), target.key


def test_a_sensor_change_converts_without_needing_the_device():
    target = next(iter(targets_for(_info(DeviceKind.SENSOR, ("temperature",)))))

    assert target.to_raw(0.1) == 10


def test_a_float_valued_sensor_change_keeps_its_precision():
    """The registry's rule, reached through the target: 1 ppm of CO2 is
    0.000001 raw, and flooring that to 1 asks for a report every million ppm."""
    target = next(iter(targets_for(_info(DeviceKind.SENSOR, ("co2",)))))

    assert target.to_raw(1.0) == pytest.approx(0.000001)


def test_a_plug_change_needs_the_device_scaling():
    """A plug's raw unit is whatever its multiplier/divisor say, so the same
    5 W means different numbers on different plugs. This is why to_raw takes
    the scaling rather than closing over a constant."""
    power = next(t for t in targets_for(_info(DeviceKind.PLUG)) if t.key == "power")

    assert power.to_raw(5.0, (1, 1)) == 5
    assert power.to_raw(5.0, (1, 10)) == 50


def test_a_plug_change_without_scaling_stays_raw():
    """Before the device has told us its scaling there is nothing to convert
    with, and inventing one would ask for reports at a rate nobody chose."""
    power = next(t for t in targets_for(_info(DeviceKind.PLUG)) if t.key == "power")

    assert power.to_raw(5.0, None) == 5


def test_a_plug_change_never_converts_to_zero():
    """A reportable change of zero asks the device to report on every sample."""
    power = next(t for t in targets_for(_info(DeviceKind.PLUG)) if t.key == "power")

    assert power.to_raw(0.4, (1, 1)) == 1


def test_a_relay_change_is_always_one():
    state = next(t for t in targets_for(_info(DeviceKind.PLUG)) if t.key == "state")

    assert state.to_raw(99.0, (1, 1000)) == 1


def test_a_coordinator_has_no_targets():
    assert targets_for(_info(DeviceKind.COORDINATOR)) == ()


def test_an_unsupported_device_has_no_targets():
    assert targets_for(_info(DeviceKind.UNSUPPORTED)) == ()


def test_a_cluster_the_registry_does_not_know_is_skipped():
    """A device carrying something we have no row for — a manufacturer cluster,
    or a quantity dropped from the registry."""
    info = _info(DeviceKind.SENSOR, ("temperature",), extra_clusters=(0xFC11,))

    assert {t.key for t in targets_for(info)} == {"temperature"}


def test_battery_is_a_target_even_though_it_does_not_classify_a_sensor():
    """The trap this cost a rewrite to find. ``measurements`` deliberately omits
    battery — a battery cluster alone does not make something a sensor — but the
    battery IS configurable, and every real sensor has one. Deriving targets
    from the classifying list left it unconfigured with nothing failing."""
    from backend.app.services.zigbee.measurements import POWER_CONFIGURATION_CLUSTER

    info = _info(DeviceKind.SENSOR, ("temperature",), extra_clusters=(POWER_CONFIGURATION_CLUSTER,))

    assert "battery" not in info.measurements, "the fixture must reproduce the real asymmetry"
    assert {t.key for t in targets_for(info)} == {"temperature", "battery", "battery_voltage"}


def test_battery_keeps_the_hour_long_defaults_the_registry_pins():
    info = _info(DeviceKind.SENSOR, ("temperature",), extra_clusters=(0x0001,))
    battery = next(t for t in targets_for(info) if t.key == "battery")

    assert (battery.min_interval, battery.max_interval) == (3600, 10800)


class TestAPlugWithOnlyMetering:
    """A plug without ElectricalMeasurement used to yield energy and NEVER
    watts, in silence — the only warning in the log was about a missing Metering
    cluster, which is a different thing entirely.

    No hardware with this profile exists here. Covered by tests only.
    """

    def test_power_falls_back_to_metering_demand(self):
        from backend.app.services.zigbee.reporting_targets import ATTR_INSTANTANEOUS_DEMAND

        power = next(t for t in targets_for(_info(DeviceKind.PLUG, has_em=False)) if t.key == "power")

        assert power.cluster == METERING
        assert power.attribute == ATTR_INSTANTANEOUS_DEMAND

    def test_electrical_measurement_still_wins_when_present(self):
        from backend.app.services.zigbee.reporting_targets import ATTR_ACTIVE_POWER

        power = next(t for t in targets_for(_info(DeviceKind.PLUG, has_em=True)) if t.key == "power")

        assert power.cluster == ELECTRICAL_MEASUREMENT
        assert power.attribute == ATTR_ACTIVE_POWER

    def test_a_plug_with_neither_source_has_no_power_target(self):
        targets = targets_for(_info(DeviceKind.PLUG, has_em=False, has_metering=False))

        assert {t.key for t in targets} == {"state"}

    def test_a_plug_without_metering_still_reports_power_and_no_energy(self):
        targets = targets_for(_info(DeviceKind.PLUG, has_em=True, has_metering=False))

        assert {t.key for t in targets} == {"state", "power"}
