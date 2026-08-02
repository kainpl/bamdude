"""What a Zigbee device is, and whether we want it.

The whole project scope lives here, and it is a classification rather than a
predicate: a device is the coordinator, a plug, a sensor, or something BamDude
cannot use. **The set is closed.** Buttons, thermostats and door sensors stay
out by decision, and anything unsupported is removed from the network rather
than kept — this is where that decision stops being an agreement and becomes
behaviour, which is what keeps the project from becoming Zigbee2MQTT.

What does grow is the *measurement registry* (``measurements.py``): a new
quantity is a row in a table, never a new class here.

Pure inspection, no I/O: everything below runs against a stub device, which is
why the gate is fully tested without a radio.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from backend.app.services.zigbee.measurements import measurement_keys_for

# Zigbee Cluster Library ids. Named rather than inlined because a bare 0x0006 in
# a conditional is exactly the sort of thing that gets "tidied" into the wrong
# constant later.
# NWK 0x0000 is the coordinator's address by Zigbee spec — a stable
# discriminator that needs no reference to the application object.
COORDINATOR_NWK = 0x0000

ON_OFF = 0x0006
METERING = 0x0702
ELECTRICAL_MEASUREMENT = 0x0B04


class DeviceKind(str, Enum):
    """What BamDude can do with a device.

    The set is closed: plugs and sensors, and nothing else will be added —
    extensibility lives in the measurement registry, not here.
    """

    COORDINATOR = "coordinator"
    PLUG = "plug"
    SENSOR = "sensor"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class DeviceInfo:
    ieee: str
    nwk: int | None
    manufacturer: str | None
    model: str | None
    kind: DeviceKind
    # Which registry quantities this device carries. Empty for plugs and for
    # anything unsupported: a plug's energy has its own scaling and its own
    # path, and is deliberately not a "measurement" in this sense.
    measurements: tuple[str, ...]
    # Recorded even though nothing reads them yet: phase 3 needs to know a plug
    # will never report energy *before* it starts treating an absent reading as
    # a measurement of zero.
    has_metering: bool
    has_electrical_measurement: bool
    reject_reason: str | None

    # Kept as properties so every existing call site — the pairing route, the
    # device list, the coordinator — keeps reading the same way it always did.
    @property
    def is_coordinator(self) -> bool:
        """The radio itself lives in zigpy's device table alongside real
        devices, and the Dongle-M reports an On/Off cluster — so this has to be
        answered explicitly or the coordinator reads as a switchable plug."""
        return self.kind is DeviceKind.COORDINATOR

    @property
    def is_plug(self) -> bool:
        return self.kind is DeviceKind.PLUG


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
    nwk = getattr(device, "nwk", None)
    # Checked BEFORE the cluster test, not after: the coordinator genuinely has
    # an On/Off cluster, so cluster presence alone would classify our own radio
    # as a plug. Found on real hardware — the dongle appeared in the device list
    # as pairable, which would have let phase 3 bind it to a printer and let
    # DELETE call remove() on the radio running the network.
    is_coordinator = nwk == COORDINATOR_NWK
    measurements = () if is_coordinator else measurement_keys_for(clusters)

    if is_coordinator:
        kind = DeviceKind.COORDINATOR
    elif ON_OFF in clusters:
        # A metering plug may also carry a temperature cluster. On/Off wins:
        # switching is what BamDude does with it, and the tie has to break
        # somewhere explicit rather than by dict ordering.
        kind = DeviceKind.PLUG
    elif measurements:
        kind = DeviceKind.SENSOR
    else:
        kind = DeviceKind.UNSUPPORTED

    return DeviceInfo(
        ieee=str(device.ieee),
        nwk=nwk,
        manufacturer=getattr(device, "manufacturer", None),
        model=getattr(device, "model", None),
        kind=kind,
        measurements=() if kind is DeviceKind.PLUG else measurements,
        has_metering=METERING in clusters,
        has_electrical_measurement=ELECTRICAL_MEASUREMENT in clusters,
        reject_reason=(
            None
            if kind in (DeviceKind.PLUG, DeviceKind.SENSOR)
            else (
                "This is the Zigbee coordinator itself, not a device on the network."
                if is_coordinator
                else "This device has no On/Off cluster, so it cannot be switched, and it reports nothing "
                "BamDude reads. BamDude pairs smart plugs and sensors only."
            )
        ),
    )


def describe_for_ui(info: DeviceInfo) -> dict:
    """A DeviceInfo as a plain JSON-safe dict.

    One place rather than ``asdict`` at each call site: ``kind`` must reach the
    WebSocket and the API as a plain string. It is a ``str`` enum, so it would
    survive JSON today by accident — this makes it deliberate, and keeps the
    payload shape in one reviewed place now that three consumers read it.
    """
    return {
        "ieee": info.ieee,
        "nwk": info.nwk,
        "manufacturer": info.manufacturer,
        "model": info.model,
        "kind": info.kind.value,
        "measurements": list(info.measurements),
        "is_plug": info.is_plug,
        "is_coordinator": info.is_coordinator,
        "has_metering": info.has_metering,
        "has_electrical_measurement": info.has_electrical_measurement,
        "reject_reason": info.reject_reason,
    }
