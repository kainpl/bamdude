"""What an operator may set on one Zigbee device, and what comes back.

Both intervals are ``uint16`` on the wire, and 65535 has a meaning of its own in
ZCL — "do not report" rather than "very rarely" — so the bounds below stop one
short of it rather than at the type's limit.
"""

from pydantic import BaseModel, Field


class TargetSettings(BaseModel):
    """One quantity's reporting parameters. Every field optional: a request that
    moves one number must leave the others to whatever they resolved to."""

    min_interval: int | None = Field(default=None, ge=0, le=65534)
    max_interval: int | None = Field(default=None, ge=0, le=65534)
    # In display units (°C, %, W, kWh) — the conversion to what a device counts
    # in happens once, in the target.
    reportable_change: float | None = Field(default=None, ge=0)


class DeviceSettingsUpdate(BaseModel):
    reporting: dict[str, TargetSettings] | None = None
    # A floor of 5 s and a ceiling of an hour: below that the radio is spent on
    # one device, above it the poll stops being a safety net at all.
    poll_seconds: int | None = Field(default=None, ge=5, le=3600)
    stale_after_seconds: int | None = Field(default=None, ge=10, le=86400)


class TargetState(BaseModel):
    """Two facts, deliberately not one word.

    ``state`` is what the device answered to the request; ``verification`` is
    what reading the configuration back said. "Accepted" and "verified" are
    different claims, and after a restart both are simply unknown.
    """

    state: str
    verification: str


class DeviceSettingsResponse(BaseModel):
    ieee: str
    kind: str
    name: str | None
    adopted: bool
    # Which fields may be changed, per target. A relay has only one of them,
    # and saying so here keeps that peculiarity out of every consumer.
    editable: dict[str, list[str]]
    desired: dict[str, TargetSettings]
    applied: dict[str, TargetState]
    poll_seconds: int
    # False for a device that sleeps between reports. Polling one is not merely
    # useless: it is timeouts on a shared radio and a flattened battery.
    poll_supported: bool
    stale_after_seconds: int
