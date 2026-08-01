"""A plug that has gone away must not be able to hang the page.

Measured on live hardware, plug pulled out of the wall. Individual HTTP status
requests sat inside Zigbee reads for 28–74 s, several at a time:

    20:51:35 -> 20:52:40   ff4b3793   >= 65s
    20:51:41 -> 20:52:55   bef7fe1d   >= 74s
    20:51:46 -> 20:52:59   dafcf75e   >= 73s

The API process itself was healthy the whole time — /health answered in 0.21 s,
the event loop was idle in select, the DB pool never grew. What died was the
browser tab: it gets ~6 connections per origin, those requests consumed them,
and it could no longer issue anything at all, including its own reload. It came
back the instant the plug did.

Two things made it that bad, and both are tested here: nobody bounded how long a
request could wait, and four viewers of the same plug started four independent
sets of reads that then queued on the one radio and slowed each other down.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from backend.app.services.zigbee.coordinator import CoordinatorState
from backend.app.services.zigbee.driver import (
    _REQUEST_REFRESH_BUDGET_SECONDS,
    ZigbeePlugData,
    ZigbeeSmartPlugService,
)


def _plug(plug_id: int = 1):
    plug = MagicMock()
    plug.id = plug_id
    plug.zigbee_ieee = "00:11:22:33:44:55:66:77"
    return plug


def _service_with_device():
    """A service whose plug resolves to a device, so ``refresh`` gets past the
    coordinator gate and actually reaches the read."""
    service = ZigbeeSmartPlugService()
    service._device_for = MagicMock(return_value=MagicMock())
    return service


class TestARequestIsBounded:
    @pytest.mark.asyncio
    @pytest.mark.timeout(20)
    async def test_a_read_that_never_returns_does_not_hold_the_caller(self):
        """The defect, stated directly: before this, the caller waited as long as
        the read did."""
        service = _service_with_device()

        async def never(*_args, **_kwargs):
            await asyncio.Event().wait()

        with patch("backend.app.services.zigbee.reporting.refresh_plug", never):
            result = await asyncio.wait_for(service.refresh(_plug(), timeout=0.05), timeout=5)

        assert result is False

        await service.cancel_refreshes()

    @pytest.mark.asyncio
    @pytest.mark.timeout(20)
    async def test_giving_up_does_not_cancel_the_read(self):
        """The impatient caller must not make things worse for everyone else.

        If a timed-out request cancelled the shared read, a user refreshing the
        page would repeatedly destroy the poller's work — and the cache would
        never be filled by the one caller that can afford to wait.
        """
        service = _service_with_device()
        finished = asyncio.Event()

        async def slow(*_args, **_kwargs):
            await asyncio.sleep(0.2)
            finished.set()
            return True

        with patch("backend.app.services.zigbee.reporting.refresh_plug", slow):
            assert await service.refresh(_plug(), timeout=0.01) is False
            await asyncio.wait_for(finished.wait(), timeout=5)

    @pytest.mark.asyncio
    @pytest.mark.timeout(20)
    async def test_a_healthy_read_is_still_awaited_and_returned(self):
        """The budget must not turn a working plug into a silent one."""
        service = _service_with_device()

        async def quick(*_args, **_kwargs):
            return True

        with patch("backend.app.services.zigbee.reporting.refresh_plug", quick):
            assert await service.refresh(_plug(), timeout=_REQUEST_REFRESH_BUDGET_SECONDS) is True

    def test_the_request_budget_is_far_below_the_browser_pile_up_point(self):
        """Six requests at this budget still cannot fill a tab's connection
        allowance for long enough to matter; six at 74 s did."""
        assert _REQUEST_REFRESH_BUDGET_SECONDS <= 5


class TestOneReadPerPlug:
    @pytest.mark.asyncio
    @pytest.mark.timeout(20)
    async def test_concurrent_callers_share_a_single_read(self):
        """Four viewers of one plug produced four sets of reads on one radio.
        They did not just duplicate work — they queued on each other, which is
        why the measured durations grew from 28 s to 74 s."""
        service = _service_with_device()
        calls = 0

        async def counted(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.05)
            return True

        plug = _plug()
        with patch("backend.app.services.zigbee.reporting.refresh_plug", counted):
            results = await asyncio.gather(*(service.refresh(plug, timeout=5) for _ in range(4)))

        assert calls == 1
        assert results == [True, True, True, True]

    @pytest.mark.asyncio
    @pytest.mark.timeout(20)
    async def test_different_plugs_are_not_serialised_behind_each_other(self):
        """Sharing is per plug. Collapsing all plugs onto one read would make a
        farm's worth of sockets wait on whichever device is worst."""
        service = _service_with_device()
        started = []

        async def counted(_service, plug, _device):
            started.append(plug.id)
            await asyncio.sleep(0.05)
            return True

        with patch("backend.app.services.zigbee.reporting.refresh_plug", counted):
            await asyncio.gather(service.refresh(_plug(1), timeout=5), service.refresh(_plug(2), timeout=5))

        assert sorted(started) == [1, 2]

    @pytest.mark.asyncio
    @pytest.mark.timeout(20)
    async def test_a_later_caller_starts_a_fresh_read(self):
        """Sharing lasts only as long as the read does — otherwise the first
        answer would be served forever."""
        service = _service_with_device()
        calls = 0

        async def counted(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            return True

        plug = _plug()
        with patch("backend.app.services.zigbee.reporting.refresh_plug", counted):
            await service.refresh(plug, timeout=5)
            await service.refresh(plug, timeout=5)

        assert calls == 2

    @pytest.mark.asyncio
    @pytest.mark.timeout(20)
    async def test_the_in_flight_entry_is_dropped_when_the_read_ends(self):
        """A leak here would pin a task object per plug forever, and worse, keep
        handing out a finished read as if it were current."""
        service = _service_with_device()

        async def quick(*_args, **_kwargs):
            return True

        with patch("backend.app.services.zigbee.reporting.refresh_plug", quick):
            await service.refresh(_plug(), timeout=5)

        assert service._refreshing == {}


class TestAFailedReadIsAttributed:
    @pytest.mark.asyncio
    @pytest.mark.timeout(20)
    async def test_a_raising_read_is_reported_not_propagated(self):
        service = _service_with_device()

        async def boom(*_args, **_kwargs):
            raise RuntimeError("radio said no")

        with patch("backend.app.services.zigbee.reporting.refresh_plug", boom):
            assert await service.refresh(_plug(), timeout=5) is False

    @pytest.mark.asyncio
    @pytest.mark.timeout(20)
    async def test_an_exception_nobody_waited_for_is_still_retrieved(self, caplog):
        """The case that produces a bare "Task exception was never retrieved" at
        garbage-collection time, detached from the plug it came from: every
        caller times out first, and the read fails afterwards."""
        service = _service_with_device()

        async def slow_boom(*_args, **_kwargs):
            await asyncio.sleep(0.05)
            raise RuntimeError("radio said no")

        with caplog.at_level("WARNING"), patch("backend.app.services.zigbee.reporting.refresh_plug", slow_boom):
            assert await service.refresh(_plug(), timeout=0.01) is False
            await asyncio.sleep(0.15)

        assert any("radio said no" in r.getMessage() for r in caplog.records)
        assert service._refreshing == {}


class TestCancelRefreshes:
    @pytest.mark.asyncio
    @pytest.mark.timeout(20)
    async def test_reads_in_flight_are_stopped_when_the_radio_goes_away(self):
        """They hold cluster objects belonging to the application being torn
        down; left running they would read against a dead radio."""
        service = _service_with_device()
        cancelled = asyncio.Event()

        async def never(*_args, **_kwargs):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        with patch("backend.app.services.zigbee.reporting.refresh_plug", never):
            asyncio.ensure_future(service.refresh(_plug(), timeout=5))  # noqa: RUF006 — cancelled below
            await asyncio.sleep(0.05)
            assert service._refreshing

            await service.cancel_refreshes()

        assert cancelled.is_set()
        assert service._refreshing == {}

    @pytest.mark.asyncio
    @pytest.mark.timeout(20)
    async def test_it_is_safe_with_nothing_running(self):
        await ZigbeeSmartPlugService().cancel_refreshes()


class TestTheStatusPathUsesTheShortBudget:
    @pytest.mark.asyncio
    @pytest.mark.timeout(20)
    async def test_a_stale_cache_refreshes_within_the_request_budget(self):
        """``get_status`` is what the browser calls, so it is the path that must
        carry the short budget rather than the poller's."""
        service = _service_with_device()
        service._cache[1] = ZigbeePlugData(state="ON")
        service._is_stale = MagicMock(return_value=True)
        seen = {}

        async def record(plug, timeout=None):
            seen["timeout"] = timeout
            return False

        service.refresh = record

        status = await service.get_status(_plug())

        assert seen["timeout"] == _REQUEST_REFRESH_BUDGET_SECONDS
        assert status["reachable"] is False

    @pytest.mark.asyncio
    @pytest.mark.timeout(20)
    async def test_a_fresh_cache_never_touches_the_radio(self):
        """The common case, and the reason the budget is rarely reached: the
        poller runs every 30–45 s against a 120 s staleness window, so a healthy
        plug is answered from cache every time."""
        service = _service_with_device()
        service._cache[1] = ZigbeePlugData(state="ON")
        service.refresh = MagicMock(side_effect=AssertionError("must not read the device"))

        status = await service.get_status(_plug())

        assert status == {"state": "ON", "reachable": True, "device_name": None}


class TestTheCoordinatorGateStillComesFirst:
    @pytest.mark.asyncio
    @pytest.mark.timeout(20)
    async def test_a_down_radio_costs_nothing_at_all(self):
        """No budget to spend, because nothing is attempted: a radio that is down
        is known to be down without asking the mesh."""
        service = ZigbeeSmartPlugService()

        with patch("backend.app.services.zigbee.driver.zigbee_coordinator") as coord:
            coord.status.state = CoordinatorState.ERROR
            coord.app = MagicMock()

            assert await service.refresh(_plug(), timeout=5) is False

        assert service._refreshing == {}
