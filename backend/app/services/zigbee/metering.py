"""Turning Zigbee cluster readings into the units BamDude records.

Isolated from the driver deliberately. This is the one calculation in the Zigbee
work whose failure mode is a **plausible** number rather than an error, and
phase 0 established what that costs: a daily counter filed as a lifetime total
made every print spanning midnight record negative energy, and nothing looked
broken until someone read the archive.

The Zigbee equivalent is worse, because the scale factor is device-chosen.
``current_summ_delivered`` is an integer whose meaning lives in two sibling
attributes on the same cluster; read it raw and file it as kWh and the archive
fills with numbers off by a factor of 1000 that look entirely reasonable.
"""

from __future__ import annotations

# Metering (0x0702) — the cumulative counter and its scaling pair.
ENERGY_SUMMATION = "current_summ_delivered"
ENERGY_MULTIPLIER = "multiplier"
ENERGY_DIVISOR = "divisor"

# ElectricalMeasurement (0x0B04) — instantaneous power and its own pair.
# Separate names, not a shared one: the two clusters scale independently and a
# device may report sane energy alongside unscaled power.
POWER_ATTR = "active_power"
POWER_MULTIPLIER = "ac_power_multiplier"
POWER_DIVISOR = "ac_power_divisor"


def scale(raw: int | float | None, multiplier: int | None, divisor: int | None) -> float | None:
    """``raw × multiplier ÷ divisor``, or None when the answer would be a guess.

    A missing **divisor** yields None rather than defaulting to 1: the device
    has not said what its counter means, and inventing a scale produces
    something that reads as a measurement to every consumer downstream.

    A missing **multiplier** does default to 1. The asymmetry is intentional —
    1 is the ZCL default for the multiplier and cannot make the result
    meaningless, while a wrong divisor changes the answer by orders of
    magnitude.

    Zero is a real reading, not a missing one: a plug that has never drawn power
    reports 0, and treating that as absent would show a healthy plug as
    unreadable.
    """
    if raw is None:
        return None
    if not divisor:  # None or 0 — both mean "unscaled, therefore unknown"
        return None
    return float(raw) * float(multiplier if multiplier else 1) / float(divisor)
