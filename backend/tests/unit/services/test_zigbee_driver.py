"""The Zigbee plug driver — the same four methods every other plug type has.

Runs entirely against stub devices: the coordinator's ``app`` is patched, so no
radio is involved. Phases 1 and 2 both had their real defects at the seam rather
than in the logic, so the stubs here are shaped from what the actual S60ZBTPF
reported — On/Off, Metering and ElectricalMeasurement on one endpoint, with the
scaling attributes present.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from zigpy.zcl import foundation

from backend.app.services.zigbee.coordinator import CoordinatorState, CoordinatorStatus
from backend.app.services.zigbee.devices import ELECTRICAL_MEASUREMENT, METERING, ON_OFF
from backend.app.services.zigbee.driver import ZigbeeSmartPlugService

IEEE = "a4:c1:38:0b:5a:9c:ff:ff"


def _cluster(attrs=None, command_status=foundation.Status.SUCCESS, cached=None, readable=True):
    """A stub cluster shaped like the real one on the two points that bit us.

    ``command`` answers with a Default Response, because a refusal does not
    raise — it comes back in that frame, and a stub returning a bare mock would
    let the driver treat every refusal as a success and never notice.

    ``get`` is the cluster's attribute cache and is the ONLY place a value comes
    from. ``read_attributes`` refreshes the device and returns nothing useful
    here on purpose: zigpy suppresses the update event for the attribute being
    read and hands back the pre-quirk raw value, so any code that trusted its
    return value would bypass every device fix. ``cached`` is therefore what the
    cache holds once the read has happened — including the case where a quirk
    swallowed the reading and the cache keeps what it had.
    """
    cache = dict(cached or {})
    cluster = SimpleNamespace(
        command=AsyncMock(return_value=[0x00, command_status]),
        _attr_cache={},
        attrs=attrs or {},
        add_listener=lambda _listener: None,
        get=lambda attr, default=None: cache.get(attr, default),
        update_attribute=lambda attr, value: cache.__setitem__(attr, value),
    )
    cluster.cache = cache
    if readable:
        cluster.read_attributes = AsyncMock(return_value=({}, {}))
    return cluster


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


def _cached(state=None, power=None, energy_total=None, age_seconds=0):
    """A cache entry, fresh unless aged on purpose.

    Stands in for ``ZigbeePlugData``, which always carries ``last_seen`` — a
    stub without it would be testing a shape the driver never sees, and
    freshness is now load-bearing.
    """
    return SimpleNamespace(
        state=state,
        power=power,
        energy_total=energy_total,
        last_seen=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
    )


def _plug(ieee=IEEE, plug_id=1):
    return SimpleNamespace(id=plug_id, name="printer plug", plug_type="zigbee", zigbee_ieee=ieee)


def _app(*devices):
    return SimpleNamespace(devices={d.ieee: d for d in devices})


@pytest.fixture
def service():
    return ZigbeeSmartPlugService()


def _with_app(app, state=CoordinatorState.UP):
    """Stand in for the coordinator singleton.

    Carries a ``status`` as well as an ``app`` because the driver now asks both:
    a lost radio leaves ``app`` in place and only moves the status, so "there is
    an app" stopped being a usable proxy for "the radio works". Pass ``state`` to
    simulate a radio that is down.
    """
    return patch(
        "backend.app.services.zigbee.driver.zigbee_coordinator",
        SimpleNamespace(app=app, status=CoordinatorStatus(state)),
    )


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
        service._cache[1] = _cached(state="ON")

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
        service._cache[1] = _cached(state="ON")

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
        service._cache[1] = _cached(state="ON")

        with _with_app(_app(_device())):
            status = await service.get_status(_plug())

        assert status["state"] == "ON"
        assert status["reachable"] is True


class TestEnergy:
    @pytest.mark.asyncio
    async def test_total_is_returned_in_kwh(self, service):
        service._cache[1] = _cached(state="ON", energy_total=12.345)

        with _with_app(_app(_device({ON_OFF: _cluster(), METERING: _cluster()}))):
            energy = await service.get_energy(_plug())

        assert energy["total"] == pytest.approx(12.345)

    @pytest.mark.asyncio
    async def test_power_is_returned_in_watts(self, service):
        service._cache[1] = _cached(state="ON", power=42.0)

        with _with_app(_app(_device({ON_OFF: _cluster(), ELECTRICAL_MEASUREMENT: _cluster()}))):
            energy = await service.get_energy(_plug())

        assert energy["power"] == pytest.approx(42.0)

    @pytest.mark.asyncio
    async def test_total_is_omitted_when_unknown_never_zeroed(self, service):
        """Only ``total`` feeds smart_plug_energy_snapshots. A plug with no
        lifetime source must be skipped there, and a zero would instead be
        differenced as real consumption."""
        service._cache[1] = _cached(state="ON", power=42.0)

        with _with_app(_app(_device({ON_OFF: _cluster()}))):
            energy = await service.get_energy(_plug())

        assert "total" not in energy

    @pytest.mark.asyncio
    async def test_zero_total_is_reported(self, service):
        """A brand-new plug legitimately reads zero."""
        service._cache[1] = _cached(state="ON", energy_total=0.0)

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


class TestStaleness:
    """A cache with no freshness bound eventually lies.

    Found on hardware and reported bluntly: the plug was switched OFF with a fan
    attached and the API kept answering ``power: 32``. It was a value read once
    at bind time and never refreshed — presented as current because ``reachable``
    only asked whether the cache had *anything* in it.

    Reporting keeps the cache fresh when it works. When it does not, a read on
    demand is the difference between an honest answer and a confident wrong one.
    """

    @pytest.mark.asyncio
    async def test_a_stale_entry_triggers_a_live_read(self, service):
        onoff = _cluster(cached={0x0000: 0})
        service._cache[1] = _cached(state="ON", power=32.0, energy_total=0.0, age_seconds=3600)

        with _with_app(_app(_device({ON_OFF: onoff}))):
            status = await service.get_status(_plug())

        onoff.read_attributes.assert_awaited()
        assert status["state"] == "OFF"

    @pytest.mark.asyncio
    async def test_a_fresh_entry_is_not_re_read(self, service):
        """Reporting is the normal path; a read per status call would be the
        polling this driver deliberately avoids."""
        onoff = _cluster(cached={0x0000: 0})
        service._cache[1] = _cached(state="ON")

        with _with_app(_app(_device({ON_OFF: onoff}))):
            status = await service.get_status(_plug())

        onoff.read_attributes.assert_not_awaited()
        assert status["state"] == "ON"

    @pytest.mark.asyncio
    async def test_a_failed_refresh_reports_unreachable_rather_than_stale(self, service):
        """The whole point: never hand back a number we cannot stand behind."""
        onoff = _cluster()
        onoff.read_attributes = AsyncMock(side_effect=OSError("no route"))
        service._cache[1] = _cached(state="ON", power=32.0, energy_total=0.0, age_seconds=3600)
        service._listeners.clear()

        with _with_app(_app(_device({ON_OFF: onoff}))):
            status = await service.get_status(_plug())

        assert status["reachable"] is False
        assert status["state"] is None

    @pytest.mark.asyncio
    async def test_stale_energy_is_withheld(self, service):
        """A stale wattage is worse than none: it reads as a measurement."""
        service._cache[1] = _cached(state="ON", power=32.0, energy_total=1.0, age_seconds=3600)

        with _with_app(_app(_device({ON_OFF: _cluster(readable=False)}))):
            energy = await service.get_energy(_plug())

        assert energy is None or "power" not in energy


class TestStateAfterSwitching:
    """The device's acknowledgement is what updates state — not a read-back.

    ZHA does the same (``Switch.async_turn_on``: check the Default Response
    status, then write the attribute through). This does not reopen phase 0's
    rule against recording a hoped-for state: the ack is the device saying it
    did the thing, and a command that is refused or unanswered still leaves the
    cache alone.

    A read-back was tried first and was actively harmful — see the power test
    below.
    """

    @pytest.mark.asyncio
    async def test_an_acknowledged_command_updates_the_state(self, service):
        onoff = _cluster()
        service._cache[1] = _cached(state="OFF")

        with _with_app(_app(_device({ON_OFF: onoff}))):
            assert await service.turn_on(_plug()) is True
            status = await service.get_status(_plug())

        assert status["state"] == "ON"

    @pytest.mark.asyncio
    async def test_a_refused_command_fails_and_leaves_the_state_alone(self, service):
        """A refusal arrives as a status in the response, not as an exception.

        Without checking it the driver would report success and cache a state
        the plug never entered.
        """
        onoff = _cluster(command_status=foundation.Status.FAILURE)
        service._cache[1] = _cached(state="OFF")

        with _with_app(_app(_device({ON_OFF: onoff}))):
            assert await service.turn_on(_plug()) is False

        assert service._cache[1].state == "OFF"

    @pytest.mark.asyncio
    async def test_switching_on_drops_the_previous_reading(self, service):
        """The reported bug, in the direction where nothing else covers it.

        The plug's power register updates on its own schedule, so any reading
        held at the moment of a switch describes the load that just went away.
        Switching ON must therefore report nothing until a real measurement
        arrives — a leftover number would read as the new load. The poller
        refills it within a cycle.

        The OFF direction is covered by the physical rule instead: an open relay
        carries nothing, so it reports zero rather than nothing.
        """
        onoff = _cluster()
        service._cache[1] = _cached(state="OFF", power=33.0, energy_total=1.0)

        with _with_app(_app(_device({ON_OFF: onoff, METERING: _cluster()}))):
            assert await service.turn_on(_plug()) is True
            energy = await service.get_energy(_plug())

        assert "power" not in (energy or {})
        assert energy["total"] == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_switching_off_reports_zero_not_the_old_load(self, service):
        onoff = _cluster()
        service._cache[1] = _cached(state="ON", power=33.0, energy_total=1.0)

        with _with_app(_app(_device({ON_OFF: onoff, METERING: _cluster()}))):
            assert await service.turn_off(_plug()) is True
            energy = await service.get_energy(_plug())

        assert energy["power"] == 0.0

    @pytest.mark.asyncio
    async def test_switching_costs_no_read(self, service):
        """The ack already carries the answer; a read-back would only add a
        round-trip to every switch."""
        onoff = _cluster()

        with _with_app(_app(_device({ON_OFF: onoff}))):
            assert await service.turn_on(_plug()) is True

        onoff.read_attributes.assert_not_awaited()


class TestPowerWhileTheSocketIsOff:
    """An open relay carries no load, whatever the device says.

    Stated as physics rather than trusted from the plug, because plugs lie about
    exactly this. Ours keeps answering with the last wattage it measured, so a
    socket with nothing running reported 33 W — twice, once from a value read at
    bind time and once from a value restored out of zigpy's database.

    The upstream quirk for that model does zero the reading, but only on a
    transition it witnesses: its OnOff half writes 0 into ``active_power`` while
    its ElectricalMeasurement half blocks every write to ``active_power`` once the
    socket reads off, so the zero lands only because the on/off cache still says
    ON at that instant. After a restart there is no transition to witness. This
    rule needs neither a transition nor a quirk for the plug at hand.
    """

    @pytest.mark.asyncio
    async def test_a_stale_wattage_is_not_reported_while_off(self, service):
        service._cache[1] = _cached(state="OFF", power=33.0, energy_total=1.0)

        with _with_app(_app(_device({ON_OFF: _cluster(), METERING: _cluster()}))):
            energy = await service.get_energy(_plug())

        assert energy["power"] == 0.0

    @pytest.mark.asyncio
    async def test_the_lifetime_counter_is_untouched_by_the_rule(self, service):
        """``total`` is a counter, not a measurement of now — and it is the only
        key that feeds per-print energy."""
        service._cache[1] = _cached(state="OFF", power=33.0, energy_total=1.234)

        with _with_app(_app(_device({ON_OFF: _cluster(), METERING: _cluster()}))):
            energy = await service.get_energy(_plug())

        assert energy["total"] == pytest.approx(1.234)

    @pytest.mark.asyncio
    async def test_an_unknown_state_does_not_claim_zero(self, service):
        """Zero is only honest when we know the relay is open. Not knowing the
        state is not the same as knowing it is off."""
        service._cache[1] = _cached(state=None, power=33.0)

        with _with_app(_app(_device({ON_OFF: _cluster()}))):
            energy = await service.get_energy(_plug())

        assert energy["power"] == pytest.approx(33.0)

    @pytest.mark.asyncio
    async def test_a_measurement_is_reported_while_on(self, service):
        service._cache[1] = _cached(state="ON", power=35.0)

        with _with_app(_app(_device({ON_OFF: _cluster()}))):
            energy = await service.get_energy(_plug())

        assert energy["power"] == pytest.approx(35.0)


class TestRadioDownCascadesToEveryPlug:
    """A dead coordinator means no plug is reachable — say so at once.

    ``connection_lost`` moves the status to ERROR but does NOT clear ``app``:
    the application object and its device table outlive the transport. The
    driver read "there is an app" as "the radio works", so after the dongle went
    away every plug kept reporting healthy for the whole staleness window — the
    cache was recent, so nothing asked the radio anything, and the card went on
    saying the plug was fine.

    Reported from the bench: a plug pulled out of the wall produced no visible
    change in the UI at all.
    """

    @pytest.mark.asyncio
    async def test_a_lost_radio_makes_a_plug_unreachable_immediately(self, service):
        device = _device({ON_OFF: AsyncMock()})
        # Fresh cache — without the status check nothing would even try to read.
        service.update(1, state="ON", power=42.0)

        with _with_app(_app(device)):
            assert (await service.get_status(_plug()))["reachable"] is True

        with _with_app(_app(device), CoordinatorState.ERROR):
            status = await service.get_status(_plug())

        assert status["reachable"] is False
        assert status["state"] is None, "an unreachable plug must not keep asserting its last state"

    @pytest.mark.asyncio
    async def test_it_sends_nothing_to_the_hardware(self, service):
        """Marking, not commanding. The devices cannot receive anything anyway,
        and a queued command would land whenever the radio returned — long after
        the operator stopped expecting it."""
        onoff = AsyncMock()
        device = _device({ON_OFF: onoff})
        service.update(1, state="ON")

        with _with_app(_app(device), CoordinatorState.ERROR):
            await service.get_status(_plug())

        onoff.command.assert_not_awaited()
        onoff.read_attributes.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", [CoordinatorState.ERROR, CoordinatorState.DISABLED, CoordinatorState.STARTING])
    async def test_only_up_counts_as_usable(self, service, state):
        """STARTING included deliberately: a radio part-way through connecting
        cannot answer for a device either."""
        device = _device({ON_OFF: AsyncMock()})
        service.update(1, state="ON")

        with _with_app(_app(device), state):
            assert (await service.get_status(_plug()))["reachable"] is False


def test_the_stale_window_keeps_headroom_above_two_poll_cycles():
    """Pinned because it is a judgement call, and because the tempting value is
    wrong.

    120 s is about three of the poller's 30-45 s cycles. 90 was tried and
    reverted: it equals exactly two worst-case cycles, so two consecutive polls
    at the top of the jitter range leave a healthy plug on the edge of being
    called unreachable. A false "offline" is worse than slow detection - the
    operator acts on offline.

    Latency is not this constant's job any more. A radio that goes down is
    reported immediately via the coordinator's status; this only covers a single
    device going quiet on a working mesh.
    """
    from backend.app.services.zigbee.driver import _STALE_AFTER_SECONDS
    from backend.app.services.zigbee.poller import _POLL_INTERVAL_SECONDS

    assert _STALE_AFTER_SECONDS == 120
    assert _POLL_INTERVAL_SECONDS[1] * 2 < _STALE_AFTER_SECONDS, (
        "must leave room above two worst-case polls, or jitter alone can fake an offline plug"
    )


class TestStalenessIsPerDevice:
    """One question — after how many seconds do we stop trusting the last value.

    Plug staleness must stay where it is. Deriving it from the poll interval
    times a multiplier gives 60–90 s instead of 120 and SHORTENS the time to
    "unreachable" — and a plug wrongly marked offline is worse than one marked
    late, because that is the reading people act on.
    """

    def test_the_default_for_a_polled_device_is_still_two_minutes(self):
        from backend.app.services.zigbee.driver import _STALE_AFTER_SECONDS

        assert _STALE_AFTER_SECONDS == 120

    def test_a_reading_inside_the_default_window_is_fresh(self):
        from datetime import datetime, timedelta, timezone

        from backend.app.services.zigbee.driver import ZigbeePlugData, ZigbeeSmartPlugService

        service = ZigbeeSmartPlugService()
        data = ZigbeePlugData(last_seen=datetime.now(timezone.utc) - timedelta(seconds=60))

        assert service._is_stale(data, 1) is False

    def test_a_device_override_shortens_the_window(self):
        from datetime import datetime, timedelta, timezone

        from backend.app.services.zigbee.driver import ZigbeePlugData, ZigbeeSmartPlugService

        service = ZigbeeSmartPlugService()
        data = ZigbeePlugData(last_seen=datetime.now(timezone.utc) - timedelta(seconds=60))
        service.set_stale_after(1, 45)

        assert service._is_stale(data, 1) is True

    def test_a_device_override_lengthens_it_too(self):
        from datetime import datetime, timedelta, timezone

        from backend.app.services.zigbee.driver import ZigbeePlugData, ZigbeeSmartPlugService

        service = ZigbeeSmartPlugService()
        data = ZigbeePlugData(last_seen=datetime.now(timezone.utc) - timedelta(seconds=200))
        service.set_stale_after(1, 600)

        assert service._is_stale(data, 1) is False

    def test_one_plug_s_setting_does_not_reach_another(self):
        from datetime import datetime, timedelta, timezone

        from backend.app.services.zigbee.driver import ZigbeePlugData, ZigbeeSmartPlugService

        service = ZigbeeSmartPlugService()
        data = ZigbeePlugData(last_seen=datetime.now(timezone.utc) - timedelta(seconds=60))
        service.set_stale_after(1, 45)

        assert service._is_stale(data, 2) is False

    def test_clearing_the_override_restores_the_default(self):
        from datetime import datetime, timedelta, timezone

        from backend.app.services.zigbee.driver import ZigbeePlugData, ZigbeeSmartPlugService

        service = ZigbeeSmartPlugService()
        data = ZigbeePlugData(last_seen=datetime.now(timezone.utc) - timedelta(seconds=60))
        service.set_stale_after(1, 45)
        service.set_stale_after(1, None)

        assert service._is_stale(data, 1) is False

    @pytest.mark.asyncio
    async def test_removing_a_plug_takes_its_override_with_it(self):
        """Otherwise the next plug to be given this id inherits a threshold
        nobody set for it."""
        from backend.app.services.zigbee.driver import ZigbeeSmartPlugService

        service = ZigbeeSmartPlugService()
        service.set_stale_after(1, 45)
        await service.teardown(1)

        assert service._stale_after == {}

    @pytest.mark.asyncio
    async def test_the_stored_threshold_reaches_the_driver_when_a_plug_is_wired(self, monkeypatch):
        """Otherwise the column is decorative: stored, shown, and never used."""
        from contextlib import asynccontextmanager
        from types import SimpleNamespace

        from backend.app.services.zigbee import reporting as module
        from backend.app.services.zigbee.driver import ZigbeeSmartPlugService

        service = ZigbeeSmartPlugService()
        device = SimpleNamespace(ieee="aa:bb", endpoints={})
        monkeypatch.setattr(service, "_device_for", lambda _plug: device)

        @asynccontextmanager
        async def fake_session():
            yield SimpleNamespace()

        async def fake_row(db, ieee):
            return SimpleNamespace(stale_after_seconds=45)

        monkeypatch.setattr("backend.app.core.database.async_session", fake_session)
        monkeypatch.setattr("backend.app.services.zigbee.device_settings.load_device_row", fake_row)

        await module.subscribe_all(service, [SimpleNamespace(id=1)])

        assert service._stale_after == {1: 45}
