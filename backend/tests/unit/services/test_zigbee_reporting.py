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
from backend.tests.zigbee_fixtures import fake_device


@pytest.fixture(autouse=True)
def _clean_attachment_registry():
    """The attachment registry is process-global, so it leaks between tests.

    Clearing it here rather than per test: a shared registry that survives is
    exactly the sort of state that makes one test pass because another ran
    first, and the failure lands on whoever adds the next test.
    """
    from backend.app.services.zigbee.reporting import _attached_clusters

    _attached_clusters.clear()
    yield
    _attached_clusters.clear()


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
        # set_stale_after is part of the service contract now; a stub without
        # it would fail inside the per-plug guard and read as "the plug failed".
        service = SimpleNamespace(
            _device_for=lambda p: SimpleNamespace(ieee=p.zigbee_ieee),
            set_stale_after=lambda plug_id, seconds: None,
        )

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
        # set_stale_after is part of the service contract now; a stub without
        # it would fail inside the per-plug guard and read as "the plug failed".
        service = SimpleNamespace(
            _device_for=lambda p: SimpleNamespace(ieee=p.zigbee_ieee),
            set_stale_after=lambda plug_id, seconds: None,
        )

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


class TestEachClusterGetsItsOwnReportingBounds:
    """One shared triple would be wrong for two of the three clusters.

    The numbers are ZHA's, and they differ on purpose: an energy counter that
    only ever grows meets "changed by one" continuously, so asking for it as
    often as power means a report every few seconds, all print long, for a
    figure read to two decimals. On/Off has no minimum because a relay cannot
    chatter by itself and the operator is waiting for exactly that event.

    Pinned rather than left to the constants: a silent drift here changes mesh
    traffic on every plug on the farm and nothing would fail.
    """

    @pytest.mark.asyncio
    async def test_the_bounds_asked_of_each_cluster_are_zhas(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from backend.app.services.zigbee.devices import ELECTRICAL_MEASUREMENT, METERING
        from backend.app.services.zigbee.driver import ZigbeeSmartPlugService
        from backend.app.services.zigbee.reporting import ATTR_ACTIVE_POWER, ATTR_SUMMATION, bind_plug

        def cluster():
            return SimpleNamespace(
                add_listener=lambda _l: None,
                on_event=lambda _name, _l: None,
                bind=AsyncMock(),
                configure_reporting=AsyncMock(return_value={}),
                read_attributes=AsyncMock(return_value=({}, {})),
                get=lambda attr, default=None: default,
            )

        clusters = {ON_OFF: cluster(), METERING: cluster(), ELECTRICAL_MEASUREMENT: cluster()}
        device = SimpleNamespace(endpoints={1: SimpleNamespace(in_clusters=clusters)})

        await bind_plug(ZigbeeSmartPlugService(), SimpleNamespace(id=1), device)

        assert clusters[ON_OFF].configure_reporting.await_args.args == (ATTR_ON_OFF, 0, 900, 1)
        assert clusters[METERING].configure_reporting.await_args.args == (ATTR_SUMMATION, 30, 900, 1)
        assert clusters[ELECTRICAL_MEASUREMENT].configure_reporting.await_args.args == (
            ATTR_ACTIVE_POWER,
            5,
            900,
            1,
        )


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


def _sensor_device(*cluster_ids):
    """Shared fixture — see backend/tests/zigbee_fixtures.py for why local
    cluster stubs are not allowed to exist here any more."""
    return fake_device("aa:bb", *cluster_ids)


@pytest.mark.asyncio
async def test_binding_a_sensor_configures_every_registered_cluster_it_has():
    from backend.app.services.zigbee.reporting import bind_sensor

    device = _sensor_device(0x0402, 0x0405, 0x0001)

    result = await bind_sensor(device, "aa:bb", parameters={})

    assert result["temperature"]["state"] == "ok"
    assert result["humidity"]["state"] == "ok"
    assert result["battery"]["state"] == "ok"


@pytest.mark.asyncio
async def test_the_reportable_change_reaches_the_device_in_raw_units():
    """0.5 degrees must arrive as 50. Sending 0.5 asks for a report on every
    flicker, which on a coin cell is a week of battery."""
    from backend.app.services.zigbee.reporting import bind_sensor

    device = _sensor_device(0x0402)

    await bind_sensor(device, "aa:bb", parameters={"temperature": {"reportable_change": 0.5}})

    assert device.endpoints[1].in_clusters[0x0402].configured == [("measured_value", 30, 900, 50)]


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
async def test_one_cluster_failing_does_not_cost_the_others():
    """Isolation between clusters. The failure here is an exception, which means
    "we never heard back" rather than "the device declined" — see
    TestReportingStateIsHonest for why the two are not the same word."""
    from backend.app.services.zigbee.reporting import bind_sensor

    device = _sensor_device(0x0402, 0x0405)

    async def blows_up(*args, **kwargs):
        raise RuntimeError("no answer")

    device.endpoints[1].in_clusters[0x0402].configure_reporting = blows_up

    result = await bind_sensor(device, "aa:bb", parameters={})

    assert result["temperature"]["state"] == "unanswered"
    assert result["humidity"]["state"] == "ok"


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


@pytest.mark.asyncio
async def test_a_report_from_a_sensor_with_stale_settings_triggers_a_re_apply():
    """A report IS the proof that a sleeper is awake.

    Reporting parameters live in the device, so a settings change reaches it
    only when it can hear us. Hanging the re-apply on the watchdog alone means
    waiting out a whole silence window — an hour at the defaults — before an
    edit takes effect, and the watchdog only runs when the device has ALREADY
    gone quiet. The moment it speaks is the moment to answer.
    """
    from backend.app.services.zigbee import reporting as module
    from backend.app.services.zigbee.coordinator import zigbee_coordinator
    from backend.app.services.zigbee.sensors import SensorStore

    rebinds: list[str] = []

    async def fake_reapply(device, ieee, keys):
        rebinds.append(ieee)

    monkey = module.reapply_if_settings_changed
    module.reapply_if_settings_changed = fake_reapply
    try:
        zigbee_coordinator.forget_reporting("aa:bb")
        device = _sensor_device(0x0402)
        listener = module.SensorReportListener(
            store=SensorStore(), ieee="aa:bb", cluster_id=0x0402, device=device, keys=("temperature",)
        )
        listener.bind_attribute(0x0000, "temperature")

        listener.attribute_updated(0x0000, 2341)
        await asyncio.sleep(0)

        assert rebinds == ["aa:bb"]
    finally:
        module.reapply_if_settings_changed = monkey
        zigbee_coordinator.forget_reporting("aa:bb")


@pytest.mark.asyncio
async def test_a_report_does_nothing_when_the_settings_already_match(monkeypatch):
    """Otherwise every report would re-issue configure_reporting, which on a
    battery device is radio traffic for nothing."""
    from contextlib import asynccontextmanager

    from backend.app.services.zigbee import reporting as module
    from backend.app.services.zigbee.coordinator import zigbee_coordinator

    settings = {"temperature": {"min_interval": 30, "max_interval": 1800, "reportable_change": 0.5}}
    binds: list[str] = []

    async def fake_bind(device, ieee, parameters):
        binds.append(ieee)
        return {"temperature": {"state": "ok", "verification": "verified"}}

    async def fake_parameters(db, info):
        return settings

    @asynccontextmanager
    async def fake_session():
        yield SimpleNamespace()

    monkeypatch.setattr(module, "bind_sensor", fake_bind)
    monkeypatch.setattr("backend.app.services.zigbee.device_settings.resolve_reporting", fake_parameters)
    monkeypatch.setattr("backend.app.core.database.async_session", fake_session)

    zigbee_coordinator.record_reporting(
        "aa:bb",
        {"temperature": settings["temperature"]},
        {"temperature": {"state": "ok", "verification": "verified"}},
    )
    try:
        await module.reapply_if_settings_changed(_sensor_device(0x0402), "aa:bb", ("temperature",))
        assert binds == [], "the device already has these parameters"

        # And the mirror: a changed setting IS pushed.
        settings["temperature"] = {"min_interval": 30, "max_interval": 60, "reportable_change": 0.5}
        await module.reapply_if_settings_changed(_sensor_device(0x0402), "aa:bb", ("temperature",))
        assert binds == ["aa:bb"]
    finally:
        zigbee_coordinator.forget_reporting("aa:bb")


@pytest.mark.asyncio
async def test_attaching_listeners_touches_no_radio():
    """Found on hardware: after a restart the sensor's reports arrived at zigpy
    and were dropped, because listeners were only ever attached at pairing. The
    device would stay blank for ever while looking perfectly paired.

    Attaching is LOCAL — no bind, no configure_reporting — which is what makes
    it safe to do for every sensor at startup, asleep or not.
    """
    from backend.app.services.zigbee.reporting import attach_sensor_listeners
    from backend.app.services.zigbee.sensors import SensorStore

    device = _sensor_device(0x0402, 0x0405)
    store = SensorStore()

    attach_sensor_listeners(device, "aa:bb", ("temperature", "humidity"), store=store)

    for cluster in device.endpoints[1].in_clusters.values():
        assert cluster.bound is False, "attaching must not talk to the device"
        assert cluster.configured == []

    listener = device.endpoints[1].in_clusters[0x0402].listeners[0]
    listener.attribute_updated(0x0000, 2341)
    assert store.reading("aa:bb", "temperature").value == pytest.approx(23.41)


@pytest.mark.asyncio
async def test_startup_attaches_every_paired_sensor_and_skips_plugs():
    from backend.app.services.zigbee.reporting import attach_all_sensors
    from backend.tests.zigbee_fixtures import BATTERY_SENSOR_FLAGS, MAINS_DEVICE_FLAGS, fake_device

    sensor = fake_device("s1", 0x0402, mac_capability_flags=BATTERY_SENSOR_FLAGS)
    plug = fake_device("p1", 0x0006, mac_capability_flags=MAINS_DEVICE_FLAGS, model="S60ZBTPF")
    app = SimpleNamespace(devices={"s1": sensor, "p1": plug})

    attached = attach_all_sensors(app)

    assert attached == 1


@pytest.mark.asyncio
async def test_a_device_report_reaches_the_store_through_the_current_event_api():
    """zigpy 2.x suppresses the legacy listener for REPORTED attributes.

    Report_Attributes handling wraps the cache write in
    _suppress_attribute_update_event, so listener_event("attribute_updated")
    never fires and the value arrives only as an emitted AttributeReportedEvent.
    Subscribing the old way makes a dead subscription look alive: the plugs have
    been running on their poller alone, and the sensor showed nothing at all.
    """
    from backend.app.services.zigbee.reporting import attach_sensor_listeners
    from backend.app.services.zigbee.sensors import SensorStore
    from backend.tests.zigbee_fixtures import fake_device

    device = fake_device("ev1", 0x0402)
    store = SensorStore()

    attach_sensor_listeners(device, "ev1", ("temperature",), store=store)
    device.endpoints[1].in_clusters[0x0402].emit_report("measured_value", 0x0000, 2341)

    assert store.reading("ev1", "temperature").value == pytest.approx(23.41)


@pytest.mark.asyncio
async def test_the_reported_value_is_taken_from_the_cache_not_the_event():
    """One rule for where a value comes from: the cluster cache, which is what a
    quirk has had its say over. The event payload is the pre-quirk report."""
    from backend.app.services.zigbee.reporting import attach_sensor_listeners
    from backend.app.services.zigbee.sensors import SensorStore
    from backend.tests.zigbee_fixtures import fake_device

    device = fake_device("ev2", 0x0402)
    cluster = device.endpoints[1].in_clusters[0x0402]
    store = SensorStore()
    attach_sensor_listeners(device, "ev2", ("temperature",), store=store)

    # The quirk left 25.00 in the cache; the raw report said 23.41.
    cluster.cache["measured_value"] = 2500
    event = SimpleNamespace(attribute_id=0x0000, attribute_name="measured_value", value=2341)
    for callback in cluster.event_callbacks["attribute_report"]:
        callback(event)

    assert store.reading("ev2", "temperature").value == pytest.approx(25.0)


@pytest.mark.asyncio
async def test_attaching_twice_leaves_one_listener():
    """bind_sensor attaches before it talks to the radio, so every re-apply of
    changed settings would add another listener to the same cluster — one more
    duplicate report each time, for the life of the process. Seen in the field
    as every first-report line printed twice.
    """
    from backend.app.services.zigbee.reporting import attach_sensor_listeners
    from backend.app.services.zigbee.sensors import SensorStore
    from backend.tests.zigbee_fixtures import fake_device

    device = fake_device("dup", 0x0402)
    cluster = device.endpoints[1].in_clusters[0x0402]
    store = SensorStore()

    attach_sensor_listeners(device, "dup", ("temperature",), store=store)
    attach_sensor_listeners(device, "dup", ("temperature",), store=store)

    assert len(cluster.event_callbacks["attribute_report"]) == 1
    assert len(cluster.listeners) == 1


@pytest.mark.asyncio
async def test_a_re_paired_device_gets_a_fresh_listener():
    """Idempotence must not outlive the device: unpairing and pairing again
    hands back new cluster objects, and the old registration would leave the
    new ones unheard."""
    from backend.app.services.zigbee.reporting import attach_sensor_listeners, forget_sensor_listeners
    from backend.app.services.zigbee.sensors import SensorStore
    from backend.tests.zigbee_fixtures import fake_device

    store = SensorStore()
    attach_sensor_listeners(fake_device("again", 0x0402), "again", ("temperature",), store=store)
    forget_sensor_listeners("again")

    fresh = fake_device("again", 0x0402)
    attach_sensor_listeners(fresh, "again", ("temperature",), store=store)

    assert len(fresh.endpoints[1].in_clusters[0x0402].event_callbacks["attribute_report"]) == 1


class TestPlugsUseTheCurrentEventApiToo:
    """The same dead channel, on the path that pays for this subsystem.

    Plugs subscribed with add_listener alone, so their "reported state ON" lines
    never came from a plug — _read_into_cache calls the listener by hand after
    each poll. configure_reporting has been decorative for them, and everything
    rested on the poller.
    """

    def test_a_plug_report_reaches_the_driver_cache(self):
        from backend.app.services.zigbee.reporting import ClusterReportListener

        service = ZigbeeSmartPlugService()
        listener = ClusterReportListener(service=service, plug_id=1, cluster_id=ON_OFF)

        listener(SimpleNamespace(attribute_id=ATTR_ON_OFF, value=1))

        assert service.get_plug_data(1).state == "ON"

    def test_an_energy_report_is_scaled_the_same_way_as_a_polled_read(self):
        """One routing, whichever channel delivered it — otherwise a report and
        a read could disagree about what the same counter means."""
        from backend.app.services.zigbee.reporting import ClusterReportListener

        service = ZigbeeSmartPlugService()
        listener = ClusterReportListener(service=service, plug_id=1, cluster_id=METERING, multiplier=1, divisor=1000)

        listener(SimpleNamespace(attribute_id=ATTR_SUMMATION, value=5000))

        assert service.get_plug_data(1).energy_total == pytest.approx(5.0)

    def test_an_event_callback_never_raises_into_zigpy(self):
        from backend.app.services.zigbee.reporting import ClusterReportListener

        listener = ClusterReportListener(service=ZigbeeSmartPlugService(), plug_id=1, cluster_id=METERING)
        listener(SimpleNamespace())  # no attribute_id at all

    @pytest.mark.asyncio
    async def test_binding_a_plug_subscribes_the_event_channel(self):
        from backend.app.services.zigbee.reporting import bind_plug
        from backend.tests.zigbee_fixtures import MAINS_DEVICE_FLAGS, fake_device

        device = fake_device("plug1", ON_OFF, mac_capability_flags=MAINS_DEVICE_FLAGS, model="S60ZBTPF")
        service = ZigbeeSmartPlugService()

        await bind_plug(service, SimpleNamespace(id=7), device)

        cluster = device.endpoints[1].in_clusters[ON_OFF]
        assert len(cluster.event_callbacks["attribute_report"]) == 1, "reports must have somewhere to land"

    @pytest.mark.asyncio
    async def test_binding_twice_does_not_double_the_subscription(self):
        from backend.app.services.zigbee.reporting import bind_plug
        from backend.tests.zigbee_fixtures import MAINS_DEVICE_FLAGS, fake_device

        device = fake_device("plug2", ON_OFF, mac_capability_flags=MAINS_DEVICE_FLAGS, model="S60ZBTPF")
        service = ZigbeeSmartPlugService()

        await bind_plug(service, SimpleNamespace(id=8), device)
        await bind_plug(service, SimpleNamespace(id=8), device)

        cluster = device.endpoints[1].in_clusters[ON_OFF]
        assert len(cluster.event_callbacks["attribute_report"]) == 1
        assert len(cluster.listeners) == 1


class TestReportingStateIsHonest:
    """ "Refused" and "we never heard back" are different facts, and the second
    one has to be retried.

    Found on hardware: a sleeping sensor went back to sleep mid-configuration,
    the battery attribute timed out, and the API said the device had REFUSED it.
    An operator reading that goes looking for a fault in the device. Worse, the
    desired state was recorded as applied anyway, so nothing ever retried and
    the attribute stayed unconfigured for good.
    """

    @pytest.mark.asyncio
    async def test_a_timeout_is_reported_as_unanswered_not_refused(self):
        from backend.app.services.zigbee.reporting import bind_sensor

        device = _sensor_device(0x0402)

        async def times_out(*args, **kwargs):
            raise TimeoutError("the device did not answer in time")

        device.endpoints[1].in_clusters[0x0402].configure_reporting = times_out

        applied = await bind_sensor(device, "aa:bb", parameters={})

        assert applied["temperature"]["state"] == "unanswered"

    @pytest.mark.asyncio
    async def test_an_explicit_non_success_status_is_reported_as_refused(self):
        """The device answered and said no. That is a different fact again, and
        it was being recorded as "ok" while the warning went only to the log."""
        from zigpy.zcl import foundation

        from backend.app.services.zigbee.reporting import bind_sensor

        device = _sensor_device(0x0402)

        async def says_no(*args, **kwargs):
            # Keyed by name: zigpy keys this by ZCLAttributeDef, and the reader
            # takes getattr(attr, "name", attr) so a plain string is equivalent
            # here — and hashable, which a SimpleNamespace is not.
            return {"measured_value": foundation.Status.UNSUPPORTED_ATTRIBUTE}

        device.endpoints[1].in_clusters[0x0402].configure_reporting = says_no

        applied = await bind_sensor(device, "aa:bb", parameters={})

        assert applied["temperature"]["state"] == "refused"

    @pytest.mark.asyncio
    async def test_an_incomplete_apply_is_retried_at_the_next_contact(self, monkeypatch):
        """A transient timeout must not mark the configuration as done."""
        from contextlib import asynccontextmanager

        from backend.app.services.zigbee import reporting as module
        from backend.app.services.zigbee.coordinator import zigbee_coordinator

        calls: list[str] = []

        async def half_applied(device, ieee, parameters):
            calls.append(ieee)
            return {
                "temperature": {"state": "ok", "verification": "verified"},
                "battery": {"state": "unanswered", "verification": "not-checked"},
            }

        async def fake_parameters(db, info):
            return {"temperature": {"min_interval": 30, "max_interval": 900, "reportable_change": 0.1}}

        @asynccontextmanager
        async def fake_session():
            yield SimpleNamespace()

        monkeypatch.setattr(module, "bind_sensor", half_applied)
        monkeypatch.setattr("backend.app.services.zigbee.device_settings.resolve_reporting", fake_parameters)
        monkeypatch.setattr("backend.app.core.database.async_session", fake_session)
        zigbee_coordinator.forget_reporting("retry")

        try:
            await module.reapply_if_settings_changed(_sensor_device(0x0402), "retry", ("temperature",))
            await module.reapply_if_settings_changed(_sensor_device(0x0402), "retry", ("temperature",))

            assert calls == ["retry", "retry"], "an unanswered attribute must be tried again"
        finally:
            zigbee_coordinator.forget_reporting("retry")


class TestPowerFromMeteringDemand:
    """Configuring reporting on the right attribute is only half of it.

    A plug with no ElectricalMeasurement now has its ``power`` target pointed at
    ``Metering.instantaneous_demand`` — but unless the listener files that
    attribute as power and the poll actually reads it, the subscription is set
    up and the value never reaches the cache. That is the exact shape of
    half-configuration this subsystem keeps rediscovering: everything looks
    wired, half the job is not done.

    No hardware with this profile exists here. Covered by tests only.
    """

    def test_a_demand_report_lands_as_power_in_watts(self):
        from backend.app.services.zigbee.reporting_targets import ATTR_INSTANTANEOUS_DEMAND

        service = ZigbeeSmartPlugService()
        # divisor 1000 means the device counts watt-hours and kilowatts; 200
        # raw is 0.2 kW, which is 200 W.
        _listener(service, METERING, multiplier=1, divisor=1000).attribute_updated(ATTR_INSTANTANEOUS_DEMAND, 200, None)

        assert service.get_plug_data(1).power == pytest.approx(200.0)

    def test_demand_and_summation_do_not_overwrite_each_other(self):
        """Both live on the same cluster with the same multiplier pair, and the
        listener dispatches on the attribute alone."""
        from backend.app.services.zigbee.reporting_targets import ATTR_INSTANTANEOUS_DEMAND

        service = ZigbeeSmartPlugService()
        listener = _listener(service, METERING, multiplier=1, divisor=1000)
        listener.attribute_updated(ATTR_SUMMATION, 5000, None)
        listener.attribute_updated(ATTR_INSTANTANEOUS_DEMAND, 200, None)

        data = service.get_plug_data(1)
        assert data.energy_total == pytest.approx(5.0)
        assert data.power == pytest.approx(200.0)

    def test_an_unusable_demand_is_not_filed_as_zero_watts(self):
        from backend.app.services.zigbee.reporting_targets import ATTR_INSTANTANEOUS_DEMAND

        service = ZigbeeSmartPlugService()
        _listener(service, METERING, multiplier=1, divisor=None).attribute_updated(ATTR_INSTANTANEOUS_DEMAND, 200, None)

        assert service.get_plug_data(1) is None or service.get_plug_data(1).power is None

    def test_demand_is_polled_only_when_there_is_no_electrical_measurement(self):
        """An extra attribute read costs a round trip on the shared radio for
        every plug on every cycle. The plugs that have ElectricalMeasurement —
        which is nearly all of them — must not pay for this fallback."""
        from backend.app.services.zigbee.reporting import metering_attrs_for

        assert metering_attrs_for(has_electrical_measurement=True) == (ATTR_SUMMATION,)
        assert len(metering_attrs_for(has_electrical_measurement=False)) == 2
