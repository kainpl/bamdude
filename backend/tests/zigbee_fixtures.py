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

    def __init__(self, cluster_id: int, asleep: bool = False, stored: tuple[int, int, int] | None = None):
        self.cluster_id = cluster_id
        # A battery device is asleep almost all of the time, so "the request
        # times out" is its NORMAL answer rather than an error case. A fixture
        # that always succeeds cannot reproduce the state the honest-reporting
        # vocabulary was built for.
        self.asleep = asleep
        # What the device claims to have stored, when a test wants the
        # read-back to answer. Off by default: most tests are about what the
        # device ANSWERED, and a stub that always confirms would quietly turn
        # every "accepted, not verified" outcome into "verified".
        self.stored = stored
        self.listeners: list = []
        self.event_callbacks: dict[str, list] = {}
        self.cache: dict[str, object] = {}
        self.configured: list = []
        self.bound = False
        self.AttributeDefs = _POWER_CONFIGURATION_DEFS if cluster_id == 0x0001 else _MEASURED_VALUE_DEFS

    def add_listener(self, listener):
        self.listeners.append(listener)

    def on_event(self, event_name, callback, with_context=False):
        """zigpy's current subscription API.

        The legacy ``add_listener``/``attribute_updated`` path is suppressed for
        reported attributes in zigpy 2.x, so a fixture without this makes a dead
        subscription look alive.
        """
        self.event_callbacks.setdefault(event_name, []).append(callback)
        return lambda: None

    def emit_report(self, attribute_name: str, attribute_id: int, value):
        """Simulate a device report the way zigpy delivers one: the cache is
        written first, then the event carries the parsed value."""
        self.cache[attribute_name] = value
        event = SimpleNamespace(
            attribute_id=attribute_id, attribute_name=attribute_name, value=value, cluster_id=self.cluster_id
        )
        for callback in self.event_callbacks.get("attribute_report", []):
            callback(event)

    async def bind(self):
        if self.asleep:
            raise TimeoutError()
        self.bound = True
        return [0]

    async def configure_reporting(self, attribute, min_interval, max_interval, change):
        if self.asleep:
            raise TimeoutError()
        self.configured.append((attribute, min_interval, max_interval, change))
        return [SimpleNamespace(status=0)]

    async def general_command(self, command_id, *args, **kwargs):
        """The read-back. zigpy has no convenience method for it, so production
        code issues Read_Reporting_Configuration by hand and so does this."""
        if self.stored is None:
            raise TimeoutError()
        from zigpy.zcl import foundation

        config = foundation.AttributeReportingConfig()
        config.direction = foundation.ReportingDirection.SendReports
        # The attribute the caller ASKED about: production code discards any
        # record whose attrid does not match, so a hardcoded zero here would
        # make every read-back look unanswerable and quietly turn a mismatch
        # into "not checked".
        records = args[0] if args else []
        config.attrid = getattr(records[0], "attrid", 0x0000) if records else 0x0000
        config.min_interval, config.max_interval, config.reportable_change = self.stored
        entry = foundation.AttributeReportingConfigWithStatus(status=foundation.Status.SUCCESS, config=config)
        return type("Rsp", (), {"attribute_configs": [entry]})()

    async def read_attributes(self, attrs, **kwargs):
        return ({}, {})

    def get(self, attr, default=None):
        return self.cache.get(attr, default)


def fake_device(
    ieee: str,
    *cluster_ids: int,
    mac_capability_flags: int = BATTERY_SENSOR_FLAGS,
    model: str = "SNZB-02D",
    manufacturer: str = "SONOFF",
    nwk: int = 0x1234,
    asleep: bool = False,
    stored: tuple[int, int, int] | None = None,
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
            1: SimpleNamespace(
                in_clusters={cid: StubCluster(cid, asleep=asleep, stored=stored) for cid in cluster_ids}
            ),
        },
        node_desc=node_descriptor(mac_capability_flags),
    )
