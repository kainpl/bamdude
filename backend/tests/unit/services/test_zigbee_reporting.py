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
            read_attributes=AsyncMock(return_value=({ATTR_ON_OFF: 1}, {})),
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
        )
        device = SimpleNamespace(endpoints={1: SimpleNamespace(in_clusters={ON_OFF: onoff})})

        wired = await bind_plug(service, SimpleNamespace(id=1), device)

        assert wired[ON_OFF] is True
