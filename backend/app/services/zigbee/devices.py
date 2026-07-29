"""What a Zigbee device is, and whether we want it.

The whole project scope lives in one predicate here: a device without an On/Off
cluster is not a plug, and BamDude does not take it. Sensors, buttons and
thermostats are out of scope by decision — this is where that decision stops
being an agreement and becomes behaviour.

Pure inspection, no I/O: everything below runs against a stub device, which is
why the gate is fully tested without a radio.
"""

from __future__ import annotations

from dataclasses import dataclass

# Zigbee Cluster Library ids. Named rather than inlined because a bare 0x0006 in
# a conditional is exactly the sort of thing that gets "tidied" into the wrong
# constant later.
ON_OFF = 0x0006
METERING = 0x0702
ELECTRICAL_MEASUREMENT = 0x0B04


@dataclass(frozen=True)
class DeviceInfo:
    ieee: str
    nwk: int | None
    manufacturer: str | None
    model: str | None
    is_plug: bool
    # Recorded even though nothing reads them yet: phase 3 needs to know a plug
    # will never report energy *before* it starts treating an absent reading as
    # a measurement of zero.
    has_metering: bool
    has_electrical_measurement: bool
    reject_reason: str | None


def _cluster_ids(device) -> set[int]:
    """Every input cluster across every endpoint.

    The union matters: a plug is free to put On/Off on endpoint 2, and several
    do. Endpoint 0 is the ZDO and carries no application clusters, so including
    it costs nothing and avoids assuming an endpoint numbering the spec does not
    actually guarantee.
    """
    ids: set[int] = set()
    for endpoint in (getattr(device, "endpoints", None) or {}).values():
        ids |= set(getattr(endpoint, "in_clusters", None) or {})
    return ids


def describe_device(device) -> DeviceInfo:
    """Identity plus the one verdict that matters: is this a plug?"""
    clusters = _cluster_ids(device)
    is_plug = ON_OFF in clusters

    return DeviceInfo(
        ieee=str(device.ieee),
        nwk=getattr(device, "nwk", None),
        manufacturer=getattr(device, "manufacturer", None),
        model=getattr(device, "model", None),
        is_plug=is_plug,
        has_metering=METERING in clusters,
        has_electrical_measurement=ELECTRICAL_MEASUREMENT in clusters,
        reject_reason=(
            None
            if is_plug
            else ("This device has no On/Off cluster, so it cannot be switched. BamDude pairs smart plugs only.")
        ),
    )
