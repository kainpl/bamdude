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
