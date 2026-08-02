"""What a Zigbee sensor can tell us, as data.

The device-class set is closed — plugs and sensors — so everything that grows
grows here: a new quantity is a row in this table, never a new class and never
another path through the driver. CO2 and PM2.5 are registered ahead of hardware
for exactly that reason; their conversions cost a row now and a re-visit later.

Every conversion below is a silent trap when wrong: the value keeps a plausible
magnitude and only its meaning changes. All of them were read out of the
attribute types in the installed zigpy rather than from memory:

* temperature 0x0402 ``int16s``  — hundredths of a degree
* humidity    0x0405 ``uint16``  — hundredths of a percent
* CO2         0x040D ``Single``  — a FRACTION, not ppm (0.0004 = 400 ppm)
* PM2.5       0x042A ``Single``  — µg/m³ as reported
* battery     0x0001/0x0021 ``uint8`` — HALF percent (200 = 100 %)
* voltage     0x0001/0x0020 ``uint8`` — 100 mV steps

There is no PM10 cluster in ZCL; zigpy defines ``PM25`` and nothing else in that
family. If a device exposes PM10 through a manufacturer cluster, that is a row
added when it can be seen on hardware, not one invented here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

TEMPERATURE_CLUSTER = 0x0402
HUMIDITY_CLUSTER = 0x0405
CO2_CLUSTER = 0x040D
PM25_CLUSTER = 0x042A
POWER_CONFIGURATION_CLUSTER = 0x0001


@dataclass(frozen=True)
class Measurement:
    """One quantity: where it lives, what it means, and how often to ask."""

    key: str
    cluster: int
    attribute: str
    unit: str
    # Display value = raw * scale. The same number converts a reportable change
    # back to raw units, which is why it is one field and not two.
    scale: float
    plausible: tuple[float, float]
    # Raw values that mean "no measurement". NaN is handled separately because
    # it is never equal to itself.
    invalid_raw: tuple[int, ...]
    default_min_interval: int
    default_max_interval: int
    default_reportable_change: float


MEASUREMENTS: tuple[Measurement, ...] = (
    Measurement(
        key="temperature",
        cluster=TEMPERATURE_CLUSTER,
        attribute="measured_value",
        unit="°C",
        scale=0.01,
        plausible=(-60.0, 150.0),
        invalid_raw=(-32768,),
        default_min_interval=30,
        default_max_interval=1800,
        default_reportable_change=0.5,
    ),
    Measurement(
        key="humidity",
        cluster=HUMIDITY_CLUSTER,
        attribute="measured_value",
        unit="%",
        scale=0.01,
        plausible=(0.0, 100.0),
        invalid_raw=(0xFFFF,),
        default_min_interval=30,
        default_max_interval=1800,
        default_reportable_change=2.0,
    ),
    Measurement(
        key="co2",
        cluster=CO2_CLUSTER,
        attribute="measured_value",
        unit="ppm",
        scale=1_000_000.0,
        plausible=(0.0, 40_000.0),
        invalid_raw=(),
        default_min_interval=30,
        default_max_interval=1800,
        default_reportable_change=50.0,
    ),
    Measurement(
        key="pm25",
        cluster=PM25_CLUSTER,
        attribute="measured_value",
        unit="µg/m³",
        scale=1.0,
        plausible=(0.0, 1000.0),
        invalid_raw=(),
        default_min_interval=30,
        default_max_interval=1800,
        default_reportable_change=5.0,
    ),
    Measurement(
        key="battery",
        cluster=POWER_CONFIGURATION_CLUSTER,
        attribute="battery_percentage_remaining",
        unit="%",
        scale=0.5,
        plausible=(0.0, 100.0),
        invalid_raw=(0xFF,),
        default_min_interval=3600,
        default_max_interval=43200,
        default_reportable_change=1.0,
    ),
    Measurement(
        key="battery_voltage",
        cluster=POWER_CONFIGURATION_CLUSTER,
        attribute="battery_voltage",
        unit="V",
        scale=0.1,
        plausible=(0.0, 10.0),
        invalid_raw=(0xFF,),
        default_min_interval=3600,
        default_max_interval=43200,
        default_reportable_change=0.1,
    ),
)

BY_KEY: dict[str, Measurement] = {m.key: m for m in MEASUREMENTS}

# The clusters that make a device a sensor. Power Configuration is excluded on
# purpose: a battery alone is not a measurement anybody paired the device for,
# and plenty of non-sensors carry one.
SENSOR_CLUSTERS: frozenset[int] = frozenset(m.cluster for m in MEASUREMENTS if m.cluster != POWER_CONFIGURATION_CLUSTER)


def measurement_keys_for(cluster_ids: set[int]) -> tuple[str, ...]:
    """Which registry quantities a device carrying these clusters can report."""
    return tuple(m.key for m in MEASUREMENTS if m.cluster in cluster_ids and m.cluster in SENSOR_CLUSTERS)


def measurements_on(cluster_id: int) -> tuple[Measurement, ...]:
    """Every quantity living on one cluster — 0x0001 carries two."""
    return tuple(m for m in MEASUREMENTS if m.cluster == cluster_id)


def to_display(measurement: Measurement, raw) -> float | None:
    """A raw attribute value as the number a human reads, or None.

    None means "no measurement": a sentinel, a NaN, something that is not a
    number at all, or a value outside physical reality. It is deliberately not
    zero — a fabricated reading is worse than a missing one.
    """
    if raw is None:
        return None
    try:
        numeric = float(raw)
    except (TypeError, ValueError):
        return None
    if math.isnan(numeric) or math.isinf(numeric):
        return None
    if numeric in measurement.invalid_raw:
        return None

    value = numeric * measurement.scale
    low, high = measurement.plausible
    if not (low <= value <= high):
        return None
    return value


def to_raw_change(measurement: Measurement, display_change: float) -> int:
    """A reportable change expressed in display units, in the raw units the
    device expects. At least 1: a change of zero asks for a report on every
    sample, which on a battery device is how a coin cell dies in a week."""
    return max(1, round(display_change / measurement.scale))
