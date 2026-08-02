"""Operator-set reporting parameters, defaulted from the registry.

One JSON blob keyed by measurement, in the existing key/value settings table —
the pattern ``drying_presets`` and ``ams_humidity_thresholds`` already use, and
the reason this cycle needs no migration.

Reporting parameters live IN the device: changing a setting here does nothing
until ``configure_reporting`` is re-issued, which happens at pairing and again
whenever the device next proves it is awake.

Every loader is total — a corrupt or nonsensical setting falls back to the
default rather than raising. These are read from a pairing callback and from a
background loop, where an exception is a feature that silently stops working.
"""

from __future__ import annotations

import json
import logging

from backend.app.api.routes.settings import get_setting
from backend.app.services.zigbee.measurements import BY_KEY, MEASUREMENTS

logger = logging.getLogger(__name__)

DEFAULT_STALE_MULTIPLIER = 2.0
DEFAULT_POLL_SECONDS = 30

_INTEGER_FIELDS = ("min_interval", "max_interval")


async def load_reporting_parameters(db) -> dict[str, dict]:
    """Desired reporting parameters per measurement, registry defaults filled in.

    ``reportable_change`` is in the measurement's display unit (°C, %, ppm,
    µg/m³); the conversion to the raw units a device expects happens once, in
    ``measurements.to_raw_change``.
    """
    parameters: dict[str, dict] = {
        m.key: {
            "min_interval": m.default_min_interval,
            "max_interval": m.default_max_interval,
            "reportable_change": m.default_reportable_change,
        }
        for m in MEASUREMENTS
    }

    raw = await get_setting(db, "zigbee_sensor_reporting")
    if not raw:
        return parameters

    try:
        stored = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("zigbee_sensor_reporting is not valid JSON — using registry defaults")
        return parameters

    if not isinstance(stored, dict):
        return parameters

    for key, overrides in stored.items():
        if key not in BY_KEY or not isinstance(overrides, dict):
            continue
        for field in (*_INTEGER_FIELDS, "reportable_change"):
            if field not in overrides:
                continue
            try:
                value = int(overrides[field]) if field in _INTEGER_FIELDS else float(overrides[field])
            except (TypeError, ValueError):
                logger.warning("zigbee_sensor_reporting.%s.%s is not a number — keeping the default", key, field)
                continue
            parameters[key][field] = value
    return parameters


async def load_stale_multiplier(db) -> float:
    """A reading older than this multiple of its window is stale."""
    raw = await get_setting(db, "zigbee_sensor_stale_multiplier")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_STALE_MULTIPLIER
    return value if value > 0 else DEFAULT_STALE_MULTIPLIER


async def load_poll_seconds(db) -> int:
    """Poll cadence for MAINS-powered sensors. Battery sensors are never polled
    on a timer — see ``sensors.power_class`` for why."""
    raw = await get_setting(db, "zigbee_sensor_poll_seconds")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_POLL_SECONDS
    return value if value > 0 else DEFAULT_POLL_SECONDS
