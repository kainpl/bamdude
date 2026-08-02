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
from backend.app.services.zigbee.measurements import BY_KEY, to_raw_change

ATTR_ON_OFF = 0x0000
ATTR_SUMMATION = 0x0000
ATTR_ACTIVE_POWER = 0x050B

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
    to_raw=_device_scaled_to_raw,
)

_POWER_TARGET = ReportingTarget(
    key="power",
    cluster=ELECTRICAL_MEASUREMENT,
    attribute=ATTR_ACTIVE_POWER,
    min_interval=5,
    max_interval=900,
    reportable_change=1,
    editable=FULLY_EDITABLE,
    to_raw=_device_scaled_to_raw,
)


def targets_for(info: DeviceInfo) -> tuple[ReportingTarget, ...]:
    """Every target this device has, in the one vocabulary both classes use."""
    if info.kind is DeviceKind.SENSOR:
        return tuple(
            ReportingTarget(
                key=key,
                cluster=BY_KEY[key].cluster,
                attribute=BY_KEY[key].attribute,
                min_interval=BY_KEY[key].default_min_interval,
                max_interval=BY_KEY[key].default_max_interval,
                reportable_change=BY_KEY[key].default_reportable_change,
                editable=FULLY_EDITABLE,
                to_raw=_sensor_to_raw(key),
            )
            for key in info.measurements
            if key in BY_KEY
        )
    if info.kind is DeviceKind.PLUG:
        return (_STATE_TARGET, _POWER_TARGET, _ENERGY_TARGET)
    return ()
