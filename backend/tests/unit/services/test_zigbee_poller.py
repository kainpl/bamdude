"""The background poll that keeps plug readings current.

Written after the driver shipped a value it had no right to call current: with
reporting as the only source, ``power`` sat at whatever was read once at bind
time, and a plug switched off kept reporting the wattage of the load it used to
carry.

Reporting is still configured and still used. It is simply not what freshness
rests on — ZHA polls the ElectricalMeasurement cluster every 30–45 s for every
device and exempts only four models known to report reliably, which is a clear
statement about how much this cluster's reporting can be trusted in the field.
"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.services.zigbee import poller as poller_module
from backend.app.services.zigbee.poller import ZigbeePoller
from backend.tests.zigbee_fixtures import BATTERY_SENSOR_FLAGS, MAINS_DEVICE_FLAGS, fake_device


def _session_factory(plugs):
    """A stand-in for ``async_session`` returning the given plug rows."""
    result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: plugs))
    db = SimpleNamespace(execute=AsyncMock(return_value=result))

    @asynccontextmanager
    async def factory():
        yield db

    return factory


@pytest.fixture
def plugs():
    return [
        SimpleNamespace(id=1, plug_type="zigbee", zigbee_ieee="aa"),
        SimpleNamespace(id=2, plug_type="zigbee", zigbee_ieee="bb"),
    ]


class TestPollCycle:
    @pytest.mark.asyncio
    async def test_every_plug_is_read(self, plugs):
        service = SimpleNamespace(refresh=AsyncMock(return_value=True))

        await ZigbeePoller()._poll_once(service, _session_factory(plugs))

        assert service.refresh.await_count == 2

    @pytest.mark.asyncio
    async def test_one_unreachable_plug_does_not_cost_the_others_their_poll(self, plugs):
        """A farm has plugs that are unplugged, out of range, or powered down by
        the very printer they control. That is normal, not a poll failure."""
        service = SimpleNamespace(refresh=AsyncMock(side_effect=[OSError("no route"), True]))

        await ZigbeePoller()._poll_once(service, _session_factory(plugs))

        assert service.refresh.await_count == 2

    @pytest.mark.asyncio
    async def test_the_plug_list_is_re_read_every_cycle(self, plugs):
        """Captured once at startup, a plug added later would never be polled
        until a restart — and "only updates on restart" is the exact symptom
        this module exists to remove."""
        service = SimpleNamespace(refresh=AsyncMock(return_value=True))
        factory = _session_factory(plugs)
        poller = ZigbeePoller()

        await poller._poll_once(service, factory)
        plugs.append(SimpleNamespace(id=3, plug_type="zigbee", zigbee_ieee="cc"))
        await poller._poll_once(service, factory)

        assert service.refresh.await_count == 5


class TestTheLoop:
    @pytest.mark.asyncio
    async def test_a_failing_cycle_does_not_end_the_loop(self, monkeypatch, plugs):
        """The one failure mode that matters here: lose the task and readings go
        back to never updating, silently and permanently."""
        monkeypatch.setattr(poller_module.random, "randint", lambda *_: 0)
        cycles = []

        async def flaky(_service, _factory):
            cycles.append(len(cycles))
            if len(cycles) == 1:
                raise OSError("radio busy")

        poller = ZigbeePoller()
        monkeypatch.setattr(poller, "_poll_once", flaky)
        poller.start(SimpleNamespace(), _session_factory(plugs))
        for _ in range(20):
            await asyncio.sleep(0)
            if len(cycles) >= 3:
                break
        await poller.stop()

        assert len(cycles) >= 3

    @pytest.mark.asyncio
    async def test_stop_awaits_the_task(self, monkeypatch, plugs):
        """Shutdown must not race a read: the poller uses the radio the
        coordinator is about to close."""
        monkeypatch.setattr(poller_module.random, "randint", lambda *_: 0)
        poller = ZigbeePoller()
        poller.start(SimpleNamespace(refresh=AsyncMock()), _session_factory(plugs))
        task = poller._task

        await poller.stop()

        assert task.done()
        assert poller._task is None

    @pytest.mark.asyncio
    async def test_starting_twice_does_not_leave_two_pollers(self, monkeypatch, plugs):
        monkeypatch.setattr(poller_module.random, "randint", lambda *_: 60)
        poller = ZigbeePoller()
        poller.start(SimpleNamespace(), _session_factory(plugs))
        first = poller._task
        poller.start(SimpleNamespace(), _session_factory(plugs))

        assert poller._task is first
        await poller.stop()

    @pytest.mark.asyncio
    async def test_stopping_a_poller_that_never_started_is_not_an_error(self):
        await ZigbeePoller().stop()


def test_the_interval_matches_the_reference_implementation():
    """30–45 s, randomised, from ZHA's AggregatedClusterPoller.

    Randomised rather than fixed for the reason ZHA randomises it: a farm's
    plugs polled in lockstep turn a trickle of mesh traffic into a burst every
    N seconds.
    """
    assert poller_module._POLL_INTERVAL_SECONDS == (30, 45)


# --- sensors (cycle S) -------------------------------------------------------
#
# The plug rule above does not transfer. A battery sensor is asleep almost
# always: polling it every cycle times out while holding the one radio and
# flattens the cell. Which mechanism applies is decided per device from
# RxOnWhenIdle, so a mains-powered sensor IS polled -- just on its own cadence.


def _sensor_device(ieee, rx_on_when_idle):
    """Built from the library's own node descriptor.

    The earlier hand-rolled stub made RxOnWhenIdle a boolean attribute, which
    the real IntFlag is not — so these two tests passed while the code called
    every device mains-powered. See backend/tests/zigbee_fixtures.py.
    """
    return fake_device(
        ieee,
        0x0402,
        mac_capability_flags=MAINS_DEVICE_FLAGS if rx_on_when_idle else BATTERY_SENSOR_FLAGS,
    )


def _app_with(*devices):
    return SimpleNamespace(devices={d.ieee: d for d in devices})


def _reads_into(sink, answered=False):
    async def _read(device, ieee, keys):
        sink.append(ieee)
        return answered

    return _read


@pytest.fixture
def sensor_db(monkeypatch):
    """Settings at their defaults, without a database.

    The resolvers are per device now, so the fakes take an ieee or a DeviceInfo
    — same values as before, so every window in these tests is unchanged and
    they still measure what they measured.
    """

    async def _parameters(db, info):
        return {"temperature": {"min_interval": 30, "max_interval": 1800, "reportable_change": 0.5}}

    async def _poll_seconds(db, ieee):
        return 30

    async def _stale_after(db, ieee, *, polled, max_interval):
        return max_interval * 2

    monkeypatch.setattr(poller_module, "resolve_reporting", _parameters)
    monkeypatch.setattr(poller_module, "resolve_poll_seconds", _poll_seconds)
    monkeypatch.setattr(poller_module, "resolve_stale_after_seconds", _stale_after)
    return SimpleNamespace()


@pytest.mark.asyncio
async def test_a_battery_sensor_with_a_fresh_reading_is_not_read(monkeypatch, sensor_db):
    """Reports are its mechanism. Reading it while it has just reported would
    spend the radio on a device that is asleep by definition."""
    reads: list[str] = []
    monkeypatch.setattr(poller_module, "read_sensor_once", _reads_into(reads))
    poller_module.sensor_store.forget("aa")
    poller_module.sensor_store.record("aa", "temperature", 2341)

    await ZigbeePoller()._poll_sensors_once(_app_with(_sensor_device("aa", False)), sensor_db)

    assert reads == []
    poller_module.sensor_store.forget("aa")


@pytest.mark.asyncio
async def test_a_silent_battery_sensor_gets_one_watchdog_read_per_window(monkeypatch, sensor_db):
    reads: list[str] = []
    monkeypatch.setattr(poller_module, "read_sensor_once", _reads_into(reads))
    poller_module.sensor_store.forget("cc")

    poller = ZigbeePoller()
    app = _app_with(_sensor_device("cc", False))
    await poller._poll_sensors_once(app, sensor_db)
    await poller._poll_sensors_once(app, sensor_db)

    assert reads == ["cc"], "the second cycle is inside the same window and must not read again"
    poller_module.sensor_store.forget("cc")


@pytest.mark.asyncio
async def test_a_mains_sensor_is_polled(monkeypatch, sensor_db):
    reads: list[str] = []
    monkeypatch.setattr(poller_module, "read_sensor_once", _reads_into(reads))
    poller_module.sensor_store.forget("bb")

    await ZigbeePoller()._poll_sensors_once(_app_with(_sensor_device("bb", True)), sensor_db)

    assert reads == ["bb"]
    poller_module.sensor_store.forget("bb")


@pytest.mark.asyncio
async def test_a_mains_sensor_polled_moments_ago_is_left_alone(monkeypatch, sensor_db):
    """This runs on every plug cycle (30-45 s). Without honouring the sensor's
    own cadence the setting would be decorative."""
    reads: list[str] = []
    monkeypatch.setattr(poller_module, "read_sensor_once", _reads_into(reads))
    poller_module.sensor_store.forget("bb")
    poller_module.sensor_store.record("bb", "temperature", 2341)

    await ZigbeePoller()._poll_sensors_once(_app_with(_sensor_device("bb", True)), sensor_db)

    assert reads == []
    poller_module.sensor_store.forget("bb")


@pytest.mark.asyncio
async def test_a_plug_is_not_treated_as_a_sensor(monkeypatch, sensor_db):
    reads: list[str] = []
    monkeypatch.setattr(poller_module, "read_sensor_once", _reads_into(reads))
    plug = SimpleNamespace(
        ieee="dd",
        nwk=0x2222,
        manufacturer="SONOFF",
        model="S60ZBTPF",
        endpoints={0: SimpleNamespace(in_clusters={}), 1: SimpleNamespace(in_clusters={0x0006: object()})},
        node_desc=None,
    )

    await ZigbeePoller()._poll_sensors_once(_app_with(plug), sensor_db)

    assert reads == []


@pytest.mark.asyncio
async def test_a_settings_change_is_pushed_when_the_device_next_answers(monkeypatch, sensor_db):
    """Reporting parameters live IN the device: changing a setting does nothing
    until configure_reporting is re-issued, and for a sleeper the only safe
    moment is when it has just proved it is awake."""
    from backend.app.services.zigbee import reporting as reporting_module

    rebinds: list[str] = []

    async def fake_bind(device, ieee, parameters):
        rebinds.append(ieee)
        return {"temperature": {"state": "ok", "verification": "verified"}}

    monkeypatch.setattr(poller_module, "read_sensor_once", _reads_into([], answered=True))
    monkeypatch.setattr(reporting_module, "bind_sensor", fake_bind)
    poller_module.sensor_store.forget("ee")
    poller_module.zigbee_coordinator.forget_reporting("ee")

    await ZigbeePoller()._poll_sensors_once(_app_with(_sensor_device("ee", False)), sensor_db)

    assert rebinds == ["ee"], "nothing had been configured yet, so the desired parameters are owed"
    poller_module.sensor_store.forget("ee")
    poller_module.zigbee_coordinator.forget_reporting("ee")


@pytest.mark.asyncio
async def test_a_read_that_times_out_still_takes_what_zigpy_cached():
    """The subsystem's own invariant, applied where it was missing.

    zigpy suppresses the update event for the attribute being READ, so a late
    answer from a sleeping device lands in the cluster cache and fires no
    listener. Giving up on the timeout and never looking at the cache is how
    zigpy ended up holding a temperature this store had never heard of.

    zigpy also restores that cache from its database at startup, so taking it
    is what gives a battery sensor its last known values immediately after a
    restart instead of a blank hour.
    """
    from backend.app.services.zigbee import poller as module

    class _LateAnswer:
        """Times out on the read, but the value is in the cache anyway."""

        AttributeDefs = ()

        async def read_attributes(self, attrs, **kwargs):
            raise TimeoutError("device did not answer in time")

        def get(self, attr, default=None):
            return 2341 if attr == "measured_value" else default

    device = SimpleNamespace(endpoints={1: SimpleNamespace(in_clusters={0x0402: _LateAnswer()})})
    module.sensor_store.forget("late")

    learned = await module.read_sensor_once(device, "late", ("temperature",))

    assert learned is True
    assert module.sensor_store.reading("late", "temperature").value == pytest.approx(23.41)
    module.sensor_store.forget("late")


@pytest.mark.asyncio
async def test_a_read_that_fails_with_nothing_cached_learns_nothing():
    """The honest other half: no answer and no cache is not a reading."""
    from backend.app.services.zigbee import poller as module

    class _Silent:
        AttributeDefs = ()

        async def read_attributes(self, attrs, **kwargs):
            raise TimeoutError("nothing")

        def get(self, attr, default=None):
            return default

    device = SimpleNamespace(endpoints={1: SimpleNamespace(in_clusters={0x0402: _Silent()})})
    module.sensor_store.forget("silent")

    learned = await module.read_sensor_once(device, "silent", ("temperature",))

    assert learned is False
    assert module.sensor_store.reading("silent", "temperature") is None
