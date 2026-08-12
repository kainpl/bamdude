"""One vocabulary for "what may I ask this device to report, and how often".

``measurements.py`` answers three questions at once: which quantities a device
carries, how a raw value becomes a display value, and how reporting is
configured for it. Only the third is shared with plugs — and for plugs the first
two work differently, because the scale is read from the device rather than
being a constant and ``on_off`` is not a number at all.

So the third is lifted out here rather than either registry being made to serve
the other. ``measurements.py`` is untouched and keeps its defaults; this module
**projects** them. Extending it to cover power, energy and state would make half
its fields optional and stop it answering its own question.

One apply loop, one desired/applied store and one API schema follow from having
one vocabulary — which is the whole reason this module exists.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from backend.app.services.zigbee.devices import (
    ELECTRICAL_MEASUREMENT,
    METERING,
    ON_OFF,
    DeviceInfo,
    DeviceKind,
)
from backend.app.services.zigbee.measurements import BY_KEY, MEASUREMENTS, to_raw_change

ATTR_ON_OFF = 0x0000
ATTR_SUMMATION = 0x0000
ATTR_ACTIVE_POWER = 0x050B
# Metering's own "how much is flowing right now". Some plugs carry this and no
# ElectricalMeasurement cluster at all, and reading only the latter left them
# with energy counted and wattage permanently blank.
ATTR_INSTANTANEOUS_DEMAND = 0x0400

FULLY_EDITABLE = ("min_interval", "max_interval", "reportable_change")

# (multiplier, divisor), as a device reports them. None means "not read yet".
Scaling = tuple[int | None, int | None]


@dataclass(frozen=True)
class ReportingTarget:
    """One attribute we ask a device to report, and the bounds we ask for."""

    key: str
    cluster: int
    # zigpy accepts either an attribute id or its name. Sensors carry names,
    # because that is what the measurement registry speaks; plug attributes are
    # ids. Resolving one into the other belongs to the cluster, which is the
    # only thing that knows the mapping for its own model.
    attribute: int | str
    min_interval: int
    max_interval: int
    # In DISPLAY units (°C, %, W, kWh). The conversion to the raw units a device
    # expects happens in ``to_raw`` and nowhere else.
    reportable_change: float
    # Which fields an operator may change. Lives here rather than in the UI so
    # that no consumer has to know ``state`` is peculiar.
    editable: tuple[str, ...]
    # What the "change by" number is measured in, for the dialog to label it.
    # Empty for a relay: it has changed or it has not, and there is no amount.
    unit: str
    to_raw: Callable[..., int | float]


def _sensor_to_raw(key: str) -> Callable[..., int | float]:
    def convert(display_change: float, scaling: Scaling | None = None) -> int | float:
        # A sensor's conversion is fixed by the registry — including the rule
        # that float-valued attributes must NOT be floored at 1. The device's
        # own scaling is irrelevant here and is accepted only so that both
        # classes share one signature.
        return to_raw_change(BY_KEY[key], display_change)

    return convert


def _device_scaled_to_raw(display_change: float, scaling: Scaling | None = None) -> int | float:
    """A plug's raw unit is whatever its multiplier and divisor say it is.

    ``scale()`` in ``metering.py`` goes the other way (raw → display), so this
    inverts it. With no scaling read yet the number is passed through rather
    than guessed: a wrong conversion here asks for reports at a rate nobody
    chose, and says nothing about it.

    Floored at 1 because these attributes are integers to the device, and a
    change of zero asks it to report on every sample it takes.
    """
    if scaling is None:
        return max(1, round(display_change))
    multiplier, divisor = scaling
    if not divisor:
        return max(1, round(display_change))
    return max(1, round(display_change * divisor / (multiplier or 1)))


def _always_one(display_change: float, scaling: Scaling | None = None) -> int:
    """A relay has changed or it has not; there is no amount of change."""
    return 1


_STATE_TARGET = ReportingTarget(
    key="state",
    cluster=ON_OFF,
    attribute=ATTR_ON_OFF,
    # No minimum: a relay cannot chatter on its own — the only thing that flips
    # it is us — and a floor only delays the confirmation of a command we just
    # sent.
    min_interval=0,
    max_interval=900,
    reportable_change=1,
    editable=("max_interval",),
    unit="",
    to_raw=_always_one,
)

_ENERGY_TARGET = ReportingTarget(
    key="energy",
    cluster=METERING,
    attribute=ATTR_SUMMATION,
    # Ten times less often than power, and for a reason: the counter only ever
    # grows, so "changed by one raw unit" is continuously true while anything is
    # drawing. On a plug with a fine divisor, five seconds means a message every
    # five seconds all print long, for a number read to two decimal places.
    min_interval=30,
    max_interval=900,
    reportable_change=1,
    editable=FULLY_EDITABLE,
    unit="kWh",
    to_raw=_device_scaled_to_raw,
)


def _power_target(info: DeviceInfo) -> ReportingTarget | None:
    """Whichever source this plug actually has, under one key.

    ElectricalMeasurement wins when present: it is what the great majority of
    plugs carry and what our scaling was built around. Metering's demand is the
    fallback, and the difference never reaches the API — a consumer asking about
    "power" should not have to know which cluster answered.
    """
    if info.has_electrical_measurement:
        cluster, attribute = ELECTRICAL_MEASUREMENT, ATTR_ACTIVE_POWER
    elif info.has_metering:
        cluster, attribute = METERING, ATTR_INSTANTANEOUS_DEMAND
    else:
        return None
    return ReportingTarget(
        key="power",
        cluster=cluster,
        attribute=attribute,
        min_interval=5,
        max_interval=900,
        reportable_change=1,
        editable=FULLY_EDITABLE,
        unit="W",
        to_raw=_device_scaled_to_raw,
    )


def targets_for(info: DeviceInfo) -> tuple[ReportingTarget, ...]:
    """Every target this device has, in the one vocabulary both classes use."""
    if info.kind is DeviceKind.SENSOR:
        # From the clusters the device actually carries, NOT from
        # ``info.measurements``. That list holds the quantities that made this a
        # sensor and omits battery by design — a battery cluster alone does not
        # make a sensor — but battery reporting is exactly the thing an operator
        # most wants to slow down. Reading the classifying list here would have
        # left every sensor's battery unconfigured, in silence.
        return tuple(
            ReportingTarget(
                key=m.key,
                cluster=m.cluster,
                attribute=m.attribute,
                min_interval=m.default_min_interval,
                max_interval=m.default_max_interval,
                reportable_change=m.default_reportable_change,
                editable=FULLY_EDITABLE,
                unit=m.unit,
                to_raw=_sensor_to_raw(m.key),
            )
            for m in MEASUREMENTS
            if m.cluster in info.cluster_ids
        )
    if info.kind is DeviceKind.PLUG:
        power = _power_target(info)
        return tuple(t for t in (_STATE_TARGET, power, _ENERGY_TARGET if info.has_metering else None) if t is not None)
    return ()
