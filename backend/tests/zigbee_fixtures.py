"""Shared builders for fake Zigbee devices in tests.

Here rather than in each test file because of a bug this repository has already
paid for once. ``NodeDescriptor.mac_capability_flags`` is an ``IntFlag``:
``flags.RxOnWhenIdle`` returns the flag MEMBER, which is always truthy, not
whether the bit is set. Three test files had independently stubbed it as an
object with a boolean attribute, so the stub answered the question the way the
implementation asked it — and every device read as mains-powered on real
hardware while the suite stayed green.

A fixture built from the library's own type cannot encode a misunderstanding of
the library. Use these builders; do not hand-roll a node descriptor.
"""

from __future__ import annotations

from types import SimpleNamespace

from zigpy.zdo import types as zdo_types

# Measured on a SONOFF SNZB-02DR2 in the field: only AllocateAddress (bit 7) is
# set. MainsPowered and RxOnWhenIdle are both clear — the device says plainly
# that it sleeps and runs off a coin cell.
BATTERY_SENSOR_FLAGS = 0b1000_0000
# FullFunctionDevice + MainsPowered + RxOnWhenIdle: a router that listens all
# the time, which is what a USB-powered sensor or a plug looks like.
MAINS_DEVICE_FLAGS = 0b0000_1110


def node_descriptor(mac_capability_flags: int, logical_type: int = 2) -> zdo_types.NodeDescriptor:
    """A real zigpy node descriptor with the given capability flags."""
    return zdo_types.NodeDescriptor(
        logical_type=logical_type,
        complex_descriptor_available=0,
        user_descriptor_available=0,
        reserved=0,
        aps_flags=0,
        frequency_band=8,
        mac_capability_flags=mac_capability_flags,
        manufacturer_code=4742,
        maximum_buffer_size=74,
        maximum_incoming_transfer_size=404,
        server_mask=10752,
        maximum_outgoing_transfer_size=404,
        descriptor_capability_field=0,
    )


_MEASURED_VALUE_DEFS = (SimpleNamespace(name="measured_value", id=0x0000),)
_POWER_CONFIGURATION_DEFS = (
    SimpleNamespace(name="battery_percentage_remaining", id=0x0021),
    SimpleNamespace(name="battery_voltage", id=0x0020),
)


class StubCluster:
    """A cluster that accepts everything and records what it was asked.

    Faithful enough to be lied to by: it carries ``AttributeDefs`` and
    ``add_listener`` because production code reads both, and a bare ``object()``
    in their place makes attachment fail silently while the assertion under test
    still passes for the wrong reason.
    """

    def __init__(self, cluster_id: int):
        self.cluster_id = cluster_id
        self.listeners: list = []
        self.configured: list = []
        self.bound = False
        self.AttributeDefs = _POWER_CONFIGURATION_DEFS if cluster_id == 0x0001 else _MEASURED_VALUE_DEFS

    def add_listener(self, listener):
        self.listeners.append(listener)

    async def bind(self):
        self.bound = True
        return [0]

    async def configure_reporting(self, attribute, min_interval, max_interval, change):
        self.configured.append((attribute, min_interval, max_interval, change))
        return [SimpleNamespace(status=0)]

    async def read_attributes(self, attrs, **kwargs):
        return ({}, {})

    def get(self, attr, default=None):
        return default


def fake_device(
    ieee: str,
    *cluster_ids: int,
    mac_capability_flags: int = BATTERY_SENSOR_FLAGS,
    model: str = "SNZB-02D",
    manufacturer: str = "SONOFF",
    nwk: int = 0x1234,
):
    """A stand-in zigpy device carrying the given input clusters.

    Endpoint 0 is the ZDO and deliberately carries nothing, mirroring a real
    device — the classifier unions every endpoint and must not assume numbering.
    """
    return SimpleNamespace(
        ieee=ieee,
        nwk=nwk,
        manufacturer=manufacturer,
        model=model,
        endpoints={
            0: SimpleNamespace(in_clusters={}),
            1: SimpleNamespace(in_clusters={cid: StubCluster(cid) for cid in cluster_ids}),
        },
        node_desc=node_descriptor(mac_capability_flags),
    )
