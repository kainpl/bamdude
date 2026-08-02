"""Attribute reports arriving from the mesh into the driver cache.

The trap this file exists for: **``OnOff.on_off`` and
``Metering.current_summ_delivered`` are both attribute id 0x0000.** A listener
that dispatches on the attribute id alone would file a plug's lifetime energy
counter as its on/off state, and the plug would report being "on" with the value
12345. So the listener has to know which cluster it is attached to, and there is
one listener per cluster rather than one per device.

Callbacks are plain ``def`` — zigpy invokes listeners with ``method(*args)`` and
never awaits, so an ``async def`` here would return a coroutine nobody runs and
every report would silently vanish. Established in phase 2, unchanged.
"""

import asyncio
from types import SimpleNamespace

import pytest

from backend.app.services.zigbee.devices import ELECTRICAL_MEASUREMENT, METERING, ON_OFF
from backend.app.services.zigbee.driver import ZigbeeSmartPlugService
from backend.app.services.zigbee.reporting import (
    ATTR_ACTIVE_POWER,
    ATTR_ON_OFF,
    ATTR_SUMMATION,
    ClusterReportListener,
)


def _listener(service, cluster_id, **scaling):
    return ClusterReportListener(service=service, plug_id=1, cluster_id=cluster_id, **scaling)


def test_callbacks_are_not_coroutine_functions():
    """Phase 2's silent trap, asserted again where it would bite again."""
    listener = _listener(ZigbeeSmartPlugService(), ON_OFF)
    assert not asyncio.iscoroutinefunction(listener.attribute_updated)


class TestTheSharedAttributeId:
    """0x0000 means different things on different clusters."""

    def test_on_off_zero_is_a_state_not_an_energy_reading(self):
        service = ZigbeeSmartPlugService()
        listener = _listener(service, ON_OFF)

        listener.attribute_updated(ATTR_ON_OFF, 1, None)

        data = service.get_plug_data(1)
        assert data.state == "ON"
        assert data.energy_total is None

    def test_metering_zero_is_an_energy_reading_not_a_state(self):
        service = ZigbeeSmartPlugService()
        listener = _listener(service, METERING, multiplier=1, divisor=1000)

        listener.attribute_updated(ATTR_SUMMATION, 12345, None)

        data = service.get_plug_data(1)
        assert data.energy_total == pytest.approx(12.345)
        assert data.state is None


class TestOnOff:
    @pytest.mark.parametrize("raw,expected", [(1, "ON"), (0, "OFF"), (True, "ON"), (False, "OFF")])
    def test_state_mapping(self, raw, expected):
        service = ZigbeeSmartPlugService()
        _listener(service, ON_OFF).attribute_updated(ATTR_ON_OFF, raw, None)

        assert service.get_plug_data(1).state == expected


class TestScaling:
    def test_energy_is_scaled_before_it_reaches_the_cache(self):
        """The cache holds kWh. Storing raw counts would push the scaling
        problem into every reader instead of solving it once."""
        service = ZigbeeSmartPlugService()
        _listener(service, METERING, multiplier=1, divisor=1000).attribute_updated(ATTR_SUMMATION, 5000, None)

        assert service.get_plug_data(1).energy_total == pytest.approx(5.0)

    def test_without_a_divisor_nothing_is_cached(self):
        """A device that never said what its counter means has told us nothing,
        and an unscaled count in the kWh field would be differenced as real
        consumption."""
        service = ZigbeeSmartPlugService()
        _listener(service, METERING, multiplier=1, divisor=None).attribute_updated(ATTR_SUMMATION, 5000, None)

        assert service.get_plug_data(1) is None or service.get_plug_data(1).energy_total is None

    def test_power_is_scaled_by_its_own_pair(self):
        service = ZigbeeSmartPlugService()
        _listener(service, ELECTRICAL_MEASUREMENT, multiplier=1, divisor=10).attribute_updated(
            ATTR_ACTIVE_POWER, 425, None
        )

        assert service.get_plug_data(1).power == pytest.approx(42.5)


class TestNoise:
    def test_an_unrelated_attribute_is_ignored(self):
        """Devices report far more than we asked for; the rest must not land
        anywhere."""
        service = ZigbeeSmartPlugService()
        _listener(service, METERING, multiplier=1, divisor=1000).attribute_updated(0x0300, 99, None)

        assert service.get_plug_data(1) is None

    def test_a_malformed_value_does_not_raise(self):
        """This runs inside zigpy's dispatch, which swallows exceptions at debug
        level — so a raising callback would not break anything, it would simply
        disappear. Handle it here where it can be logged."""
        service = ZigbeeSmartPlugService()
        _listener(service, METERING, multiplier=1, divisor=1000).attribute_updated(ATTR_SUMMATION, "not a number", None)


class TestSubscriptionIsActuallyWired:
    """The gap the hardware pass found.

    ``bind_plug`` existed and was never called, so commands worked — they go
    straight to the cluster — while status and energy stayed empty forever. The
    plug switched on and reported ``{state: null, reachable: false}``.

    A driver whose commands work and whose readings never arrive is the worst
    shape of this bug: it looks configured.
    """

    @pytest.mark.asyncio
    async def test_subscribe_all_binds_every_zigbee_plug(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from backend.app.services.zigbee import reporting

        plugs = [
            SimpleNamespace(id=1, plug_type="zigbee", zigbee_ieee="aa"),
            SimpleNamespace(id=2, plug_type="zigbee", zigbee_ieee="bb"),
        ]
        service = SimpleNamespace(_device_for=lambda p: SimpleNamespace(ieee=p.zigbee_ieee))

        with patch.object(reporting, "bind_plug", AsyncMock(return_value={})) as bind:
            await reporting.subscribe_all(service, plugs)

        assert bind.await_count == 2

    @pytest.mark.asyncio
    async def test_a_plug_whose_device_is_absent_is_skipped_not_fatal(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from backend.app.services.zigbee import reporting

        plugs = [SimpleNamespace(id=1, plug_type="zigbee", zigbee_ieee="gone")]
        service = SimpleNamespace(_device_for=lambda p: None)

        with patch.object(reporting, "bind_plug", AsyncMock()) as bind:
            await reporting.subscribe_all(service, plugs)

        bind.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_one_plug_failing_does_not_stop_the_others(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from backend.app.services.zigbee import reporting

        plugs = [
            SimpleNamespace(id=1, plug_type="zigbee", zigbee_ieee="aa"),
            SimpleNamespace(id=2, plug_type="zigbee", zigbee_ieee="bb"),
        ]
        service = SimpleNamespace(_device_for=lambda p: SimpleNamespace(ieee=p.zigbee_ieee))

        with patch.object(reporting, "bind_plug", AsyncMock(side_effect=[OSError("boom"), {}])) as bind:
            await reporting.subscribe_all(service, plugs)

        assert bind.await_count == 2


class TestInitialRead:
    """Reporting is about CHANGES. It says nothing about the current state.

    Found on hardware: binding succeeded, the log said "reporting set up for
    1/1 plug(s)", and status stayed {state: null, reachable: false} because no
    report had happened yet. Subscribing is not the same as knowing.
    """

    @pytest.mark.asyncio
    async def test_binding_seeds_the_cache_with_a_read(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from backend.app.services.zigbee.driver import ZigbeeSmartPlugService
        from backend.app.services.zigbee.reporting import bind_plug

        service = ZigbeeSmartPlugService()
        onoff = SimpleNamespace(
            add_listener=lambda _l: None,
            bind=AsyncMock(),
            configure_reporting=AsyncMock(return_value={}),
            read_attributes=AsyncMock(return_value=({}, {})),
            # The value comes from the cluster's cache, never from what the read
            # returned — that is where quirks have had their say.
            get=lambda attr, default=None: {ATTR_ON_OFF: 1}.get(attr, default),
        )
        device = SimpleNamespace(endpoints={1: SimpleNamespace(in_clusters={ON_OFF: onoff})})

        await bind_plug(service, SimpleNamespace(id=1), device)

        assert service.get_plug_data(1).state == "ON"

    @pytest.mark.asyncio
    async def test_a_failed_initial_read_is_not_fatal(self):
        """An unreadable device should still get its reporting subscription."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from backend.app.services.zigbee.driver import ZigbeeSmartPlugService
        from backend.app.services.zigbee.reporting import bind_plug

        service = ZigbeeSmartPlugService()
        onoff = SimpleNamespace(
            add_listener=lambda _l: None,
            bind=AsyncMock(),
            configure_reporting=AsyncMock(return_value={}),
            read_attributes=AsyncMock(side_effect=OSError("device asleep")),
            get=lambda attr, default=None: default,
        )
        device = SimpleNamespace(endpoints={1: SimpleNamespace(in_clusters={ON_OFF: onoff})})

        wired = await bind_plug(service, SimpleNamespace(id=1), device)

        assert wired[ON_OFF] is True


class _Attr:
    """Hashable stand-in for ZCLAttributeDef.

    SimpleNamespace defines __eq__ and is therefore unhashable, so it cannot be
    a dict key — and configure_reporting keys its result by attribute.
    """

    def __init__(self, name):
        self.name = name


class TestReportingRefusalIsNoticed:
    """The check that was written against the wrong shape and saw nothing.

    ``configure_reporting`` returns a **dict** of {attribute: Status}. The first
    version of this check iterated it as a list of records and read ``.status``
    off each element — iterating a dict yields keys, which have no such
    attribute, so it silently never fired. A blind check is worse than none: it
    reads as evidence the device accepted.
    """

    def test_a_refusal_is_reported(self, caplog):
        from zigpy.zcl import foundation

        from backend.app.services.zigbee.reporting import _warn_if_reporting_refused

        attr = _Attr("on_off")
        with caplog.at_level("WARNING"):
            _warn_if_reporting_refused(1, ON_OFF, {attr: foundation.Status.UNSUPPORTED_ATTRIBUTE})

        assert "refused" in caplog.text.lower()
        assert "on_off" in caplog.text

    def test_success_is_quiet(self, caplog):
        from zigpy.zcl import foundation

        from backend.app.services.zigbee.reporting import _warn_if_reporting_refused

        attr = _Attr("on_off")
        with caplog.at_level("WARNING"):
            _warn_if_reporting_refused(1, ON_OFF, {attr: foundation.Status.SUCCESS})

        assert "refused" not in caplog.text.lower()

    def test_an_unexpected_shape_is_logged_not_swallowed(self, caplog):
        """Silently returning on a shape we did not expect is how the first
        version hid its own bug."""
        from backend.app.services.zigbee.reporting import _warn_if_reporting_refused

        with caplog.at_level("DEBUG"):
            _warn_if_reporting_refused(1, ON_OFF, "not a mapping at all")

        assert caplog.text


class TestBindRefusalIsNoticed:
    """``bind()`` returns its ZDO status; it does not raise.

    The counterpart to the configure_reporting check. A device that refuses the
    binding has nowhere to send reports, so the whole subscription is silent —
    and every log line still reads as success. This is the first thing to look at
    when the cache only moves on a restart.
    """

    def test_a_refused_binding_is_reported(self, caplog):
        from zigpy.zdo import types as zdo_types

        from backend.app.services.zigbee.reporting import _warn_if_bind_refused

        with caplog.at_level("WARNING"):
            _warn_if_bind_refused(1, ON_OFF, [zdo_types.Status.NOT_SUPPORTED])

        assert "refused the binding" in caplog.text

    def test_a_successful_binding_is_quiet(self, caplog):
        from zigpy.zdo import types as zdo_types

        from backend.app.services.zigbee.reporting import _warn_if_bind_refused

        with caplog.at_level("WARNING"):
            _warn_if_bind_refused(1, ON_OFF, [zdo_types.Status.SUCCESS])

        assert caplog.text == ""

    def test_an_unfamiliar_shape_is_not_assumed_good(self, caplog):
        from backend.app.services.zigbee.reporting import _warn_if_bind_refused

        with caplog.at_level("WARNING"):
            _warn_if_bind_refused(1, ON_OFF, None)

        assert "refused the binding" in caplog.text


class TestScalingIsReadOnce:
    """Multiplier and divisor are device constants.

    An on-demand read that rebuilt its listener would re-read both every time —
    two extra round-trips per cluster on the shared radio, for a number that
    never changes. The bind-time listener already holds them.
    """

    @pytest.mark.asyncio
    async def test_refresh_reuses_the_bind_time_listener(self):
        from unittest.mock import AsyncMock

        from backend.app.services.zigbee.metering import ENERGY_DIVISOR, ENERGY_MULTIPLIER
        from backend.app.services.zigbee.reporting import bind_plug, refresh_plug

        service = ZigbeeSmartPlugService()
        metering = SimpleNamespace(
            add_listener=lambda _l: None,
            bind=AsyncMock(return_value=[0]),
            configure_reporting=AsyncMock(return_value={}),
            read_attributes=AsyncMock(return_value=({ENERGY_MULTIPLIER: 1, ENERGY_DIVISOR: 1000}, {})),
            get=lambda attr, default=None: {ATTR_SUMMATION: 5000}.get(attr, default),
        )
        device = SimpleNamespace(endpoints={1: SimpleNamespace(in_clusters={METERING: metering})})
        plug = SimpleNamespace(id=1)

        await bind_plug(service, plug, device)
        scaling_reads_after_bind = sum(
            1 for call in metering.read_attributes.await_args_list if ENERGY_MULTIPLIER in call.args[0]
        )
        assert service.get_plug_data(1).energy_total == pytest.approx(5.0)

        await refresh_plug(service, plug, device)
        scaling_reads_total = sum(
            1 for call in metering.read_attributes.await_args_list if ENERGY_MULTIPLIER in call.args[0]
        )

        assert scaling_reads_after_bind == 1
        assert scaling_reads_total == 1


class TestQuirksAreNotBypassed:
    """The bug that cost a whole hardware session.

    The plug's firmware keeps reporting the last measured power after its socket
    is switched off, so a socket with nothing plugged in answered 33 W. The quirk
    for that model swallows the reading — but it does so **between**
    ``read_attributes`` returning and the cluster cache being written, and the
    raw pre-quirk value stays in the return value.

    So reading the return value walks straight past every device fix, and does it
    silently: the number is well-formed, plausible, and wrong. The value has to
    come from the cluster's cache, which is where the quirk had its say.
    """

    @pytest.mark.asyncio
    async def test_the_swallowed_value_is_not_what_gets_cached(self):
        from unittest.mock import AsyncMock

        from backend.app.services.zigbee.metering import POWER_DIVISOR, POWER_MULTIPLIER
        from backend.app.services.zigbee.reporting import bind_plug

        service = ZigbeeSmartPlugService()
        em = SimpleNamespace(
            add_listener=lambda _l: None,
            bind=AsyncMock(return_value=[0]),
            configure_reporting=AsyncMock(return_value={}),
            # What the device answered: the stale 33 W from the load it used to
            # carry.
            read_attributes=AsyncMock(
                side_effect=lambda attrs, **kw: (
                    ({POWER_MULTIPLIER: 1, POWER_DIVISOR: 1}, {})
                    if POWER_MULTIPLIER in attrs
                    else ({ATTR_ACTIVE_POWER: 33}, {})
                )
            ),
            # What the quirk left in the cache: zero, because the socket is off.
            get=lambda attr, default=None: {ATTR_ACTIVE_POWER: 0}.get(attr, default),
        )
        device = SimpleNamespace(endpoints={1: SimpleNamespace(in_clusters={ELECTRICAL_MEASUREMENT: em})})

        await bind_plug(service, SimpleNamespace(id=1), device)

        assert service.get_plug_data(1).power == 0


# --- sensors (cycle S) -------------------------------------------------------
#
# Sensors reuse this module rather than copying it: bind, configure and listen
# are the part of the subsystem where a mistake is invisible -- not "the plug
# will not switch" but "the number looks right and is not". One implementation,
# two routes.


class _RecordingCluster:
    """A cluster that accepts everything and remembers what it was asked."""

    def __init__(self, cluster_id, attribute_defs=()):
        self.cluster_id = cluster_id
        self.listeners = []
        self.configured = []
        self.bound = False
        self.AttributeDefs = attribute_defs

    def add_listener(self, listener):
        self.listeners.append(listener)

    async def bind(self):
        self.bound = True
        return [0]

    async def configure_reporting(self, attribute, min_interval, max_interval, change):
        self.configured.append((attribute, min_interval, max_interval, change))
        return [SimpleNamespace(status=0)]

    def get(self, attr, default=None):
        return default


def _attr(name, attr_id):
    return SimpleNamespace(name=name, id=attr_id)


_MEASURED_VALUE = (_attr("measured_value", 0x0000),)
_BATTERY_ATTRS = (_attr("battery_percentage_remaining", 0x0021), _attr("battery_voltage", 0x0020))


def _sensor_device(*cluster_ids):
    clusters = {}
    for cluster_id in cluster_ids:
        defs = _BATTERY_ATTRS if cluster_id == 0x0001 else _MEASURED_VALUE
        clusters[cluster_id] = _RecordingCluster(cluster_id, defs)
    return SimpleNamespace(endpoints={1: SimpleNamespace(in_clusters=clusters)}, ieee="aa:bb")


@pytest.mark.asyncio
async def test_binding_a_sensor_configures_every_registered_cluster_it_has():
    from backend.app.services.zigbee.reporting import bind_sensor

    device = _sensor_device(0x0402, 0x0405, 0x0001)

    result = await bind_sensor(device, "aa:bb", parameters={})

    assert result["temperature"] == "ok"
    assert result["humidity"] == "ok"
    assert result["battery"] == "ok"


@pytest.mark.asyncio
async def test_the_reportable_change_reaches_the_device_in_raw_units():
    """0.5 degrees must arrive as 50. Sending 0.5 asks for a report on every
    flicker, which on a coin cell is a week of battery."""
    from backend.app.services.zigbee.reporting import bind_sensor

    device = _sensor_device(0x0402)

    await bind_sensor(device, "aa:bb", parameters={"temperature": {"reportable_change": 0.5}})

    assert device.endpoints[1].in_clusters[0x0402].configured == [("measured_value", 30, 1800, 50)]


@pytest.mark.asyncio
async def test_operator_intervals_override_the_registry_defaults():
    from backend.app.services.zigbee.reporting import bind_sensor

    device = _sensor_device(0x0402)

    await bind_sensor(
        device,
        "aa:bb",
        parameters={"temperature": {"min_interval": 60, "max_interval": 600, "reportable_change": 1.0}},
    )

    assert device.endpoints[1].in_clusters[0x0402].configured == [("measured_value", 60, 600, 100)]


@pytest.mark.asyncio
async def test_a_cluster_that_refuses_does_not_cost_the_others():
    from backend.app.services.zigbee.reporting import bind_sensor

    device = _sensor_device(0x0402, 0x0405)

    async def refuse(*args, **kwargs):
        raise RuntimeError("device said no")

    device.endpoints[1].in_clusters[0x0402].configure_reporting = refuse

    result = await bind_sensor(device, "aa:bb", parameters={})

    assert result["temperature"] == "refused"
    assert result["humidity"] == "ok"


@pytest.mark.asyncio
async def test_battery_and_voltage_share_a_cluster_and_are_told_apart_by_attribute():
    """0x0001 carries both. Dispatching on the cluster alone would file a
    voltage as a percentage -- the same shape of bug as 0x0000 on plugs."""
    from backend.app.services.zigbee.reporting import bind_sensor
    from backend.app.services.zigbee.sensors import sensor_store

    device = _sensor_device(0x0001)
    sensor_store.forget("aa:bb")

    await bind_sensor(device, "aa:bb", parameters={})
    listener = device.endpoints[1].in_clusters[0x0001].listeners[0]
    listener.attribute_updated(0x0021, 200)
    listener.attribute_updated(0x0020, 30)

    assert sensor_store.reading("aa:bb", "battery").value == pytest.approx(100.0)
    assert sensor_store.reading("aa:bb", "battery_voltage").value == pytest.approx(3.0)
    sensor_store.forget("aa:bb")


def test_a_sensor_report_lands_in_the_store_scaled():
    from backend.app.services.zigbee.reporting import SensorReportListener
    from backend.app.services.zigbee.sensors import SensorStore

    store = SensorStore()
    listener = SensorReportListener(store=store, ieee="aa:bb", cluster_id=0x0402)
    listener.bind_attribute(0x0000, "temperature")

    listener.attribute_updated(0x0000, 2341)

    assert store.reading("aa:bb", "temperature").value == pytest.approx(23.41)


def test_an_unasked_attribute_is_ignored():
    """Devices report far more than they were asked for."""
    from backend.app.services.zigbee.reporting import SensorReportListener
    from backend.app.services.zigbee.sensors import SensorStore

    store = SensorStore()
    listener = SensorReportListener(store=store, ieee="aa:bb", cluster_id=0x0402)
    listener.bind_attribute(0x0000, "temperature")

    listener.attribute_updated(0x0055, 1)

    assert store.known_ieees() == ()


def test_a_sensor_listener_never_raises_into_zigpy():
    """zigpy calls listeners synchronously and logs their exceptions at DEBUG,
    so a raising listener disappears instead of failing loudly."""
    from backend.app.services.zigbee.reporting import SensorReportListener

    class _Exploding:
        def record(self, *args, **kwargs):
            raise RuntimeError("store is broken")

    listener = SensorReportListener(store=_Exploding(), ieee="aa:bb", cluster_id=0x0402)
    listener.bind_attribute(0x0000, "temperature")

    listener.attribute_updated(0x0000, 2341)  # must not raise


def test_the_plug_listener_still_routes_state_energy_and_power():
    """Regression: the plug path is what pays for this subsystem. Generalising
    the module must not move a single plug reading."""
    service = ZigbeeSmartPlugService()

    _listener(service, ON_OFF).attribute_updated(ATTR_ON_OFF, 1)
    _listener(service, METERING, multiplier=1, divisor=1000).attribute_updated(ATTR_SUMMATION, 5000)
    _listener(service, ELECTRICAL_MEASUREMENT, multiplier=1, divisor=10).attribute_updated(ATTR_ACTIVE_POWER, 231)

    data = service.get_plug_data(1)
    assert data.state == "ON"
    assert data.energy_total == pytest.approx(5.0)
    assert data.power == pytest.approx(23.1)
