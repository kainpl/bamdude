"""The Zigbee plug driver — the same four methods every other plug type has.

Runs entirely against stub devices: the coordinator's ``app`` is patched, so no
radio is involved. Phases 1 and 2 both had their real defects at the seam rather
than in the logic, so the stubs here are shaped from what the actual S60ZBTPF
reported — On/Off, Metering and ElectricalMeasurement on one endpoint, with the
scaling attributes present.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services.zigbee.devices import ELECTRICAL_MEASUREMENT, METERING, ON_OFF
from backend.app.services.zigbee.driver import ZigbeeSmartPlugService

IEEE = "a4:c1:38:0b:5a:9c:ff:ff"


def _cluster(attrs=None):
    return SimpleNamespace(command=AsyncMock(), _attr_cache={}, attrs=attrs or {})


def _device(clusters=None, ieee=IEEE, endpoint=1):
    """A stub zigpy device carrying the given clusters on one endpoint."""
    clusters = clusters if clusters is not None else {ON_OFF: _cluster()}
    return SimpleNamespace(
        ieee=ieee,
        nwk=0xF6B4,
        manufacturer="SONOFF",
        model="S60ZBTPF",
        endpoints={0: SimpleNamespace(in_clusters={}), endpoint: SimpleNamespace(in_clusters=clusters)},
    )


def _plug(ieee=IEEE, plug_id=1):
    return SimpleNamespace(id=plug_id, name="printer plug", plug_type="zigbee", zigbee_ieee=ieee)


def _app(*devices):
    return SimpleNamespace(devices={d.ieee: d for d in devices})


@pytest.fixture
def service():
    return ZigbeeSmartPlugService()


def _with_app(app):
    return patch("backend.app.services.zigbee.driver.zigbee_coordinator", SimpleNamespace(app=app))


class TestSwitching:
    @pytest.mark.asyncio
    async def test_turn_on_issues_the_cluster_command(self, service):
        onoff = _cluster()
        device = _device({ON_OFF: onoff})

        with _with_app(_app(device)):
            assert await service.turn_on(_plug()) is True

        onoff.command.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_turn_off_issues_the_cluster_command(self, service):
        onoff = _cluster()
        device = _device({ON_OFF: onoff})

        with _with_app(_app(device)):
            assert await service.turn_off(_plug()) is True

        onoff.command.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_switching_an_absent_device_returns_false_without_raising(self, service):
        with _with_app(_app()):
            assert await service.turn_on(_plug()) is False

    @pytest.mark.asyncio
    async def test_switching_without_a_radio_returns_false(self, service):
        with _with_app(None):
            assert await service.turn_off(_plug()) is False

    @pytest.mark.asyncio
    async def test_a_plug_with_no_ieee_is_unreachable_rather_than_an_error(self, service):
        with _with_app(_app(_device())):
            assert await service.turn_on(_plug(ieee=None)) is False

    @pytest.mark.asyncio
    async def test_a_command_failure_leaves_the_cached_state_untouched(self, service):
        """Phase 0's rule: a comms failure must never change recorded state.

        Reporting a state we only hoped for is how automation ends up believing
        a printer is powered when it is not.
        """
        onoff = _cluster()
        onoff.command.side_effect = OSError("no route to device")
        service._cache[1] = SimpleNamespace(state="ON", power=None, energy_total=None)

        with _with_app(_app(_device({ON_OFF: onoff}))):
            assert await service.turn_off(_plug()) is False

        assert service._cache[1].state == "ON"


class TestToggle:
    @pytest.mark.asyncio
    async def test_toggle_refuses_when_the_state_is_unknown(self, service):
        """Carried over from phase 0, and it matters more here.

        The other drivers toggle blind because their transport answers
        synchronously. Here the state comes from a report cache that may be
        empty, so toggling would be a coin flip on cutting a running print.
        """
        with _with_app(_app(_device())):
            assert await service.toggle(_plug()) is False

    @pytest.mark.asyncio
    async def test_toggle_turns_a_known_on_state_off(self, service):
        onoff = _cluster()
        service._cache[1] = SimpleNamespace(state="ON", power=None, energy_total=None)

        with _with_app(_app(_device({ON_OFF: onoff}))):
            assert await service.toggle(_plug()) is True

        onoff.command.assert_awaited_once()


class TestStatus:
    @pytest.mark.asyncio
    async def test_absent_device_reports_unreachable(self, service):
        with _with_app(_app()):
            status = await service.get_status(_plug())

        assert status["reachable"] is False
        assert status["state"] is None

    @pytest.mark.asyncio
    async def test_a_bound_plug_with_no_report_yet_is_unreachable_not_broken(self, service):
        """The window between binding and the first report is honest, not a fault."""
        with _with_app(_app(_device())):
            status = await service.get_status(_plug())

        assert status["reachable"] is False

    @pytest.mark.asyncio
    async def test_a_reported_state_is_returned(self, service):
        service._cache[1] = SimpleNamespace(state="ON", power=None, energy_total=None)

        with _with_app(_app(_device())):
            status = await service.get_status(_plug())

        assert status["state"] == "ON"
        assert status["reachable"] is True


class TestEnergy:
    @pytest.mark.asyncio
    async def test_total_is_returned_in_kwh(self, service):
        service._cache[1] = SimpleNamespace(state="ON", power=None, energy_total=12.345)

        with _with_app(_app(_device({ON_OFF: _cluster(), METERING: _cluster()}))):
            energy = await service.get_energy(_plug())

        assert energy["total"] == pytest.approx(12.345)

    @pytest.mark.asyncio
    async def test_power_is_returned_in_watts(self, service):
        service._cache[1] = SimpleNamespace(state="ON", power=42.0, energy_total=None)

        with _with_app(_app(_device({ON_OFF: _cluster(), ELECTRICAL_MEASUREMENT: _cluster()}))):
            energy = await service.get_energy(_plug())

        assert energy["power"] == pytest.approx(42.0)

    @pytest.mark.asyncio
    async def test_total_is_omitted_when_unknown_never_zeroed(self, service):
        """Only ``total`` feeds smart_plug_energy_snapshots. A plug with no
        lifetime source must be skipped there, and a zero would instead be
        differenced as real consumption."""
        service._cache[1] = SimpleNamespace(state="ON", power=42.0, energy_total=None)

        with _with_app(_app(_device({ON_OFF: _cluster()}))):
            energy = await service.get_energy(_plug())

        assert "total" not in energy

    @pytest.mark.asyncio
    async def test_zero_total_is_reported(self, service):
        """A brand-new plug legitimately reads zero."""
        service._cache[1] = SimpleNamespace(state="ON", power=None, energy_total=0.0)

        with _with_app(_app(_device({ON_OFF: _cluster(), METERING: _cluster()}))):
            energy = await service.get_energy(_plug())

        assert energy["total"] == 0.0

    @pytest.mark.asyncio
    async def test_no_cache_entry_yields_none(self, service):
        with _with_app(_app(_device())):
            assert await service.get_energy(_plug()) is None


class TestClusterLocation:
    @pytest.mark.asyncio
    async def test_on_off_on_a_later_endpoint_is_found(self, service):
        """Mirrors phase 2: the cluster is not guaranteed to be on endpoint 1."""
        onoff = _cluster()
        device = _device({ON_OFF: onoff}, endpoint=3)

        with _with_app(_app(device)):
            assert await service.turn_on(_plug()) is True

        onoff.command.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ieee_matching_is_case_insensitive(self, service):
        """zigpy stringifies EUI64 lower-case; an operator may paste upper."""
        onoff = _cluster()

        with _with_app(_app(_device({ON_OFF: onoff}))):
            assert await service.turn_on(_plug(ieee=IEEE.upper())) is True
