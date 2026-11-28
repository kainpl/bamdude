"""A drying timer the printer holds but does not run is not a drying cycle.

Upstream #2896 / #3096 (6c0292b09). A printer can accept a drying command, keep
its countdown and not heat - the power budget is spent (an H2D with two AMS-HT
already drying parked the third at its full 720 minutes), or a print is
running. Read as "``dry_time`` > 0 means drying", that timer showed an amber
bar on a cold unit, held the queue for good, muted the temperature alarm and
kept auto-drying away from the unit until somebody pressed Stop.

The MQTT client is the only place that sees time pass between two reports, so
it stamps ``dry_countdown_stalled`` on the unit; everything else asks
``utils/ams_drying``.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import backend.app.models.printer_location  # noqa: F401
from backend.app.services import print_scheduler as ps
from backend.app.services.bambu_mqtt import BambuMQTTClient, PrinterState
from backend.app.services.printer_manager import printer_state_to_dict
from backend.app.utils import ams_drying

PARKED = {"id": 0, "dry_time": 720, "dry_status": 0, "dry_countdown_stalled": True}
TICKING = {"id": 1, "dry_time": 45, "dry_status": 2, "dry_countdown_stalled": False}


class TestThePredicate:
    def test_a_parked_timer_is_parked_and_not_active(self):
        assert ams_drying.is_countdown_parked(PARKED) is True
        assert ams_drying.is_drying_active(PARKED) is False
        assert ams_drying.running_dry_minutes(PARKED) == 0

    def test_a_ticking_cycle_is_active_and_not_parked(self):
        assert ams_drying.is_countdown_parked(TICKING) is False
        assert ams_drying.is_drying_active(TICKING) is True
        assert ams_drying.running_dry_minutes(TICKING) == 45

    def test_a_stale_flag_on_a_unit_with_no_timer_is_nothing(self):
        unit = {"dry_time": 0, "dry_countdown_stalled": True}
        assert ams_drying.is_countdown_parked(unit) is False
        assert ams_drying.is_drying_active(unit) is False

    def test_a_unit_the_client_never_stamped_reads_as_before(self):
        assert ams_drying.is_countdown_parked({"dry_time": 30}) is False
        assert ams_drying.is_drying_active({"dry_time": 30}) is True

    def test_garbage_does_not_raise(self):
        assert ams_drying.is_countdown_parked(None) is False
        assert ams_drying.is_countdown_parked({"dry_time": "x", "dry_countdown_stalled": True}) is False
        assert ams_drying.running_dry_minutes({"dry_time": "x"}) == 0

    def test_only_parked_needs_a_parked_timer_and_nothing_live(self):
        assert ams_drying.only_parked_timers([PARKED]) is True
        assert ams_drying.only_parked_timers([PARKED, {"id": 2, "dry_time": 0}]) is True
        # One real cycle beside it: the printer IS drying.
        assert ams_drying.only_parked_timers([PARKED, TICKING]) is False
        # A cooling tail (no countdown, live phase) is still a cycle.
        assert ams_drying.only_parked_timers([PARKED, {"id": 2, "dry_time": 0, "dry_status": 3}]) is False
        # No timer reported yet: a command just sent is drying about to begin.
        assert ams_drying.only_parked_timers([{"id": 0, "dry_time": 0}]) is False
        assert ams_drying.only_parked_timers([]) is False
        assert ams_drying.only_parked_timers(None) is False


@pytest.fixture
def client():
    return BambuMQTTClient(ip_address="192.168.1.100", serial_number="TEST-PARKED", access_code="12345678")


def _unit(client, ams_id=0):
    return next(u for u in client.state.raw_data["ams"] if int(u["id"]) == ams_id)


def _later(client, seconds: float) -> None:
    """Let ``seconds`` pass for the detector without touching the process clock."""
    client._dry_countdown_seen = {k: (value, at - seconds) for k, (value, at) in client._dry_countdown_seen.items()}


def _push(client, *, dry_time, info=None, ams_id=0, **kw):
    unit = {"id": str(ams_id), "dry_time": dry_time, "tray": []}
    if info is not None:
        unit["info"] = info
    client._handle_ams_data({"ams": [unit]}, **kw)


class TestTheMqttClientStampsIt:
    def test_a_countdown_frozen_past_the_threshold_is_parked(self, client):
        _push(client, dry_time=720)
        assert _unit(client)["dry_countdown_stalled"] is False
        _later(client, 149)
        _push(client, dry_time=720)
        assert _unit(client)["dry_countdown_stalled"] is False
        _later(client, 2)
        _push(client, dry_time=720)
        assert _unit(client)["dry_countdown_stalled"] is True
        assert ams_drying.is_countdown_parked(_unit(client)) is True

    def test_a_ticking_countdown_is_never_parked(self, client):
        """Repeats of one value inside the minute do not count as ticks, and
        do not make it parked either."""
        for value in (720, 719, 719, 718, 718, 717):
            _later(client, 55)
            _push(client, dry_time=value)
            assert _unit(client)["dry_countdown_stalled"] is False, value

    def test_a_value_change_clears_it(self, client):
        _push(client, dry_time=720)
        _later(client, 151)
        _push(client, dry_time=720)
        assert _unit(client)["dry_countdown_stalled"] is True
        _push(client, dry_time=719)
        assert _unit(client)["dry_countdown_stalled"] is False

    def test_a_live_phase_outranks_a_frozen_countdown(self, client):
        _push(client, dry_time=720, info="20")  # bits 4-7 = 2, Drying
        _later(client, 300)
        _push(client, dry_time=720, info="20")
        assert _unit(client)["dry_countdown_stalled"] is False

    def test_a_live_phase_clears_it_on_a_transient_zero(self, client):
        _push(client, dry_time=720)
        _later(client, 151)
        _push(client, dry_time=720)
        assert _unit(client)["dry_countdown_stalled"] is True
        _push(client, dry_time=0, info="10")  # Checking, the firmware's transient zero
        assert _unit(client)["dry_countdown_stalled"] is False

    def test_a_frame_without_the_countdown_neither_raises_nor_refreshes(self, client):
        _push(client, dry_time=720)
        _later(client, 151)
        # A tray-only partial: the merged unit still carries 720, but this
        # frame said nothing about the countdown.
        client._handle_ams_data({"ams": [{"id": "0", "tray": []}]})
        assert _unit(client)["dry_countdown_stalled"] is False
        # ... and it did not restart the wait: the next real report is parked.
        _push(client, dry_time=720)
        assert _unit(client)["dry_countdown_stalled"] is True

    def test_the_flag_survives_a_frame_without_the_countdown(self, client):
        _push(client, dry_time=720)
        _later(client, 151)
        _push(client, dry_time=720)
        client._handle_ams_data({"ams": [{"id": "0", "tray": []}]})
        assert _unit(client)["dry_countdown_stalled"] is True

    def test_a_finished_cycle_is_not_parked(self, client):
        _push(client, dry_time=1)
        _later(client, 200)
        _push(client, dry_time=0)
        assert _unit(client)["dry_countdown_stalled"] is False

    def test_a_command_acknowledgement_is_not_a_report(self, client):
        """Only a status frame may raise or clear it (a command's echo is not
        the printer's status)."""
        _push(client, dry_time=720)
        _later(client, 151)
        _push(client, dry_time=720, presence_is_status=False)
        assert _unit(client)["dry_countdown_stalled"] is False
        _push(client, dry_time=720)
        assert _unit(client)["dry_countdown_stalled"] is True
        _push(client, dry_time=719, presence_is_status=False)
        assert _unit(client)["dry_countdown_stalled"] is True

    def test_a_new_command_starts_a_new_wait(self, client):
        """A start sent over a parked timer of the same length changes nothing
        the detector could see - the value stays 720 - so the command itself
        has to restart the wait, or the new cycle reads parked from its first
        report on a unit that sends no phase."""
        client._client = MagicMock()
        _push(client, dry_time=720)
        _later(client, 151)
        _push(client, dry_time=720)
        assert _unit(client)["dry_countdown_stalled"] is True

        assert client.send_drying_command(0, 45, 12, mode=1, filament="PLA") is True

        assert _unit(client)["dry_countdown_stalled"] is False
        _push(client, dry_time=720)
        assert _unit(client)["dry_countdown_stalled"] is False
        # ... and a timer that then does not move is parked again.
        _later(client, 151)
        _push(client, dry_time=720)
        assert _unit(client)["dry_countdown_stalled"] is True

    def test_units_are_judged_apart(self, client):
        client._handle_ams_data(
            {"ams": [{"id": "0", "dry_time": 720, "tray": []}, {"id": "1", "dry_time": 300, "tray": []}]}
        )
        _later(client, 151)
        client._handle_ams_data(
            {"ams": [{"id": "0", "dry_time": 720, "tray": []}, {"id": "1", "dry_time": 298, "tray": []}]}
        )
        assert _unit(client, 0)["dry_countdown_stalled"] is True
        assert _unit(client, 1)["dry_countdown_stalled"] is False


def _state(units) -> PrinterState:
    state = PrinterState()
    state.connected = True
    state.raw_data = {"ams": units}
    return state


class TestBothShapersCarryIt:
    UNITS = [
        {"id": "0", "dry_time": 720, "dry_countdown_stalled": True, "tray": []},
        {"id": "1", "dry_time": 45, "dry_countdown_stalled": False, "tray": []},
        {"id": "2", "dry_time": 0, "tray": []},  # never stamped: False, never absent
    ]

    def test_the_websocket_payload(self):
        payload = printer_state_to_dict(_state(self.UNITS), printer_id=1, model="H2D")
        assert [u["dry_countdown_stalled"] for u in payload["ams"]] == [True, False, False]

    @pytest.mark.asyncio
    async def test_the_rest_status(self, async_client, printer_factory):
        printer = await printer_factory(model="H2D")
        with patch("backend.app.api.routes.printers.printer_manager") as pm:
            pm.get_status.return_value = _state(self.UNITS)
            pm.is_awaiting_plate_clear.return_value = False
            pm.get_drying_targets.return_value = {}
            response = await async_client.get(f"/api/v1/printers/{printer.id}/status")
        assert response.status_code == 200, response.text
        assert [u["dry_countdown_stalled"] for u in response.json()["ams"]] == [True, False, False]

    def test_the_flag_alone_changes_the_broadcast_key(self):
        """It flips exactly when nothing else moves - that is what "frozen"
        means - so without it in the key the card would never hear of it."""
        from backend.app.main import _printer_status_effect_key

        before = _printer_status_effect_key(_state([{"id": "0", "dry_time": 720, "tray": []}]))
        after = _printer_status_effect_key(
            _state([{"id": "0", "dry_time": 720, "dry_countdown_stalled": True, "tray": []}])
        )
        assert before != after


class TestTheTemperatureAlarm:
    def test_a_parked_unit_does_not_mute_it(self):
        from datetime import datetime, timezone

        suppress, latch = ams_drying.temperature_alarm_suppressed(
            drying_active=ams_drying.is_drying_active(PARKED),
            temperature=60.0,
            threshold=35.0,
            latched_at=None,
            now=datetime.now(timezone.utc),
            grace_minutes=30,
        )
        assert (suppress, latch) == (False, None)


def _status(*units):
    return SimpleNamespace(raw_data={"ams": list(units)}, firmware_version="01.03.00.00", state="IDLE")


class TestTheSchedulerDoesNotWaitOnIt:
    def test_the_forecast_ignores_a_parked_unit(self, monkeypatch):
        scheduler = ps.PrintScheduler()
        scheduler._drying_in_progress[1] = 1.0
        monkeypatch.setattr(ps.printer_manager, "get_status", lambda pid: _status(PARKED, TICKING))
        assert scheduler.drying_remaining_seconds(1) == 45 * 60
        monkeypatch.setattr(ps.printer_manager, "get_status", lambda pid: _status(PARKED))
        assert scheduler.drying_remaining_seconds(1) == 0

    @pytest.mark.asyncio
    async def test_a_parked_only_printer_is_not_held_for_the_router(self, monkeypatch):
        scheduler = ps.PrintScheduler()
        scheduler._drying_in_progress = {1: 1.0, 2: 1.0, 3: 1.0}
        statuses = {1: _status(PARKED), 2: _status(PARKED, TICKING), 3: _status({"id": 0, "dry_time": 0})}
        monkeypatch.setattr(ps.printer_manager, "get_status", lambda pid: statuses[pid])
        monkeypatch.setattr(scheduler, "_get_bool_setting", AsyncMock(return_value=True))
        # 1: parked only - free. 2: one real cycle - held. 3: nothing reported
        # yet, a command just sent - held.
        assert await scheduler.drying_held_printer_ids(MagicMock()) == {2, 3}


def _settings_db(**overrides):
    """The settings ``_check_auto_drying`` reads, answered by key."""
    values = {
        "queue_drying_enabled": "false",
        "ambient_drying_enabled": "true",
        "print_drying_enabled": "false",
        "queue_drying_block": "false",
        "ams_humidity_fair": "60",
        "auto_drying_sustained_minutes": "0",
        **overrides,
    }

    async def execute(statement, *args, **kwargs):
        result = MagicMock()
        try:
            params = list(statement.compile(compile_kwargs={"literal_binds": False}).params.values())
        except Exception:
            params = []
        for key, value in values.items():
            if key in params:
                result.scalar_one_or_none.return_value = SimpleNamespace(value=value)
                return result
        if "printer" in str(statement).lower():
            printer = MagicMock()
            printer.id = 1
            scalars = MagicMock()
            scalars.__iter__ = MagicMock(return_value=iter([printer]))
            result.scalars.return_value = scalars
            return result
        result.scalar_one_or_none.return_value = None
        return result

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=execute)
    return db


def _wet(**unit):
    return {
        "id": 0,
        "module_type": "n3f",
        "humidity_raw": "80",
        "dry_sf_reason": [],
        "tray": [{"tray_type": "PLA"}],
        **unit,
    }


@pytest.fixture
def auto(monkeypatch):
    """A scheduler over one connected, idle, drying-capable printer."""
    scheduler = ps.PrintScheduler()
    pm = MagicMock()
    pm.is_connected.return_value = True
    pm.get_model.return_value = "H2D"
    pm.send_drying_command.return_value = True
    pm.get_all_statuses.return_value = {}
    monkeypatch.setattr(ps, "printer_manager", pm)
    monkeypatch.setattr(ps, "supports_drying", lambda model, firmware: True)
    monkeypatch.setattr(scheduler, "_is_printer_idle", MagicMock(return_value=True))
    scheduler.pm = pm
    return scheduler


class TestAutoDryingAndAParkedUnit:
    @pytest.mark.asyncio
    async def test_it_does_not_adopt_a_hold_from_a_parked_unit(self, auto):
        auto.pm.get_status.return_value = _status(_wet(dry_time=720, dry_countdown_stalled=True))

        await auto._check_auto_drying(_settings_db(), [], set())

        assert 1 not in auto._drying_in_progress
        # Somebody else's timer: never stopped, never armed over.
        auto.pm.send_drying_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_running_cycle_is_still_adopted(self, auto):
        auto.pm.get_status.return_value = _status(_wet(dry_time=720))

        await auto._check_auto_drying(_settings_db(), [], set())

        assert 1 in auto._drying_in_progress

    @pytest.mark.asyncio
    async def test_a_unit_it_armed_itself_is_stopped_and_cools_off(self, auto):
        auto._armed_dry_cycles[(1, 0)] = time.monotonic() - 600
        auto._drying_in_progress[1] = time.monotonic() - 600
        auto._dry_unit_history[(1, 0)] = {"unproductive": 0, "suspended": False, "ended_at": None, "running": True}
        auto.pm.get_status.return_value = _status(_wet(dry_time=720, dry_countdown_stalled=True))

        await auto._check_auto_drying(_settings_db(), [], set())

        auto.pm.send_drying_command.assert_called_once_with(1, 0, 0, 0, mode=0)
        assert (1, 0) not in auto._armed_dry_cycles
        history = auto._dry_unit_history[(1, 0)]
        assert "running" not in history
        assert isinstance(history["ended_at"], float)  # the ordinary re-arm cooldown is running
        # A cycle the printer would not run is a strike: nothing else bounds the retry. It says
        # nothing about humidity, so it has its own counter (upstream #3478).
        assert history["never_heated"] == 1
        assert history.get("unproductive", 0) == 0

        # The timer is gone on the next report; still wet, but inside the cooldown.
        auto.pm.send_drying_command.reset_mock()
        auto.pm.get_status.return_value = _status(_wet(dry_time=0))
        await auto._check_auto_drying(_settings_db(), [], set())
        auto.pm.send_drying_command.assert_not_called()
        assert history["never_heated"] == 1  # the stop is counted once, not again when the timer goes
        assert history.get("unproductive", 0) == 0

    @pytest.mark.asyncio
    async def test_a_unit_the_printer_keeps_parking_is_suspended_and_reported(self, auto):
        """Two cycles in a row the printer parked: the suspension and its not-heating
        notification end the start + stop pair, and a dry reading lifts it as ever."""
        auto._notify_auto_drying_suspended = AsyncMock()
        parked = _status(_wet(dry_time=720, dry_countdown_stalled=True))
        idle = _status(_wet(dry_time=0))

        def cooled_off():
            auto._dry_unit_history[(1, 0)]["ended_at"] = time.monotonic() - ps._AUTO_DRY_REARM_COOLDOWN_SECONDS - 1

        starts = []
        for _ in range(2):
            auto.pm.get_status.return_value = idle
            await auto._check_auto_drying(_settings_db(), [], set())  # arms
            starts.append([c for c in auto.pm.send_drying_command.call_args_list if c.kwargs.get("mode") == 1])
            auto.pm.get_status.return_value = parked
            await auto._check_auto_drying(_settings_db(), [], set())  # parked: stopped
            cooled_off()
        assert [len(s) for s in starts] == [1, 2]
        assert auto._dry_unit_history[(1, 0)]["never_heated"] == 2
        assert auto._dry_unit_history[(1, 0)].get("unproductive", 0) == 0

        auto.pm.send_drying_command.reset_mock()
        auto.pm.get_status.return_value = idle
        await auto._check_auto_drying(_settings_db(), [], set())
        await auto._check_auto_drying(_settings_db(), [], set())

        auto.pm.send_drying_command.assert_not_called()
        assert auto._dry_unit_history[(1, 0)]["suspended"] is True
        auto._notify_auto_drying_suspended.assert_awaited_once()
        # The unit did not heat: the message says so, not «the threshold cannot be reached».
        assert auto._notify_auto_drying_suspended.await_args.kwargs["never_heated"] is True
        assert auto._notify_auto_drying_suspended.await_args.args[-1] == 2

        # Humidity back at or below the threshold resumes it, as for any suspension.
        auto.pm.get_status.return_value = _status(_wet(dry_time=0, humidity_raw="20"))
        await auto._check_auto_drying(_settings_db(), [], set())
        assert not auto._dry_unit_history[(1, 0)].get("suspended")
        assert "unproductive" not in auto._dry_unit_history[(1, 0)]
        assert "never_heated" not in auto._dry_unit_history[(1, 0)]

    @pytest.mark.asyncio
    async def test_a_stop_by_a_person_or_a_dispatch_is_still_no_strike(self, auto):
        auto._dry_unit_history[(1, 0)] = {"unproductive": 0, "suspended": False, "ended_at": None, "running": True}

        auto.forget_auto_dry_cycle(1, 0)

        assert auto._dry_unit_history[(1, 0)]["unproductive"] == 0

    @pytest.mark.asyncio
    async def test_a_stop_by_a_person_leaves_the_parked_count_as_it_is(self, auto):
        """A cycle cut short proves neither that the unit heats nor that it does not."""
        auto._dry_unit_history[(1, 0)] = {"never_heated": 1, "suspended": False, "ended_at": None, "running": True}

        auto.forget_auto_dry_cycle(1, 0)

        assert auto._dry_unit_history[(1, 0)]["never_heated"] == 1

    @pytest.mark.asyncio
    async def test_a_parked_strike_and_a_cycle_that_dried_nothing_do_not_add_up(self, auto):
        """Two counters, as upstream #3478: one of each is not two in a row."""
        auto._notify_auto_drying_suspended = AsyncMock()
        auto._armed_dry_cycles[(1, 0)] = time.monotonic() - 600
        auto._dry_unit_history[(1, 0)] = {
            "unproductive": 1,
            "best_end_humidity": 80,
            "suspended": False,
            "ended_at": None,
            "running": True,
        }
        auto.pm.get_status.return_value = _status(_wet(dry_time=720, dry_countdown_stalled=True))
        await auto._check_auto_drying(_settings_db(), [], set())  # parked: stopped, one strike

        auto._dry_unit_history[(1, 0)]["ended_at"] = time.monotonic() - ps._AUTO_DRY_REARM_COOLDOWN_SECONDS - 1
        auto.pm.send_drying_command.reset_mock()
        auto.pm.get_status.return_value = _status(_wet(dry_time=0))
        await auto._check_auto_drying(_settings_db(), [], set())

        history = auto._dry_unit_history[(1, 0)]
        assert (history["unproductive"], history["never_heated"]) == (1, 1)
        assert not history.get("suspended")
        auto._notify_auto_drying_suspended.assert_not_awaited()
        assert [c for c in auto.pm.send_drying_command.call_args_list if c.kwargs.get("mode") == 1]  # re-armed

    @pytest.mark.asyncio
    async def test_a_cycle_that_runs_to_its_end_resets_the_parked_count(self, auto):
        auto._dry_unit_history[(1, 0)] = {"never_heated": 1, "suspended": False, "ended_at": None, "running": True}
        auto.pm.get_status.return_value = _status(_wet(dry_time=0))

        await auto._check_auto_drying(_settings_db(), [], set())

        assert "never_heated" not in auto._dry_unit_history[(1, 0)]

    @pytest.mark.asyncio
    async def test_the_not_heating_suspension_gives_the_not_heating_advice(self, auto, caplog):
        auto._notify_auto_drying_suspended = AsyncMock()
        auto._dry_unit_history[(1, 0)] = {"never_heated": 2, "suspended": False, "ended_at": None}
        auto.pm.get_status.return_value = _status(_wet(dry_time=0))

        with caplog.at_level("WARNING", logger=ps.logger.name):
            await auto._check_auto_drying(_settings_db(), [], set())

        assert auto._dry_unit_history[(1, 0)]["suspended"] is True
        text = caplog.text
        assert "did not heat" in text and "Stop Drying on the printer card" in text
        assert "Raise the threshold" not in text
        auto.pm.send_drying_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_manual_parked_timer_beside_an_armed_unit_is_left_alone(self, auto):
        """``inv-drying-stop-scope``: only the unit auto-drying armed is its to stop."""
        auto._armed_dry_cycles[(1, 0)] = time.monotonic() - 600
        auto._dry_unit_history[(1, 0)] = {"unproductive": 0, "suspended": False, "ended_at": None, "running": True}
        auto.pm.get_status.return_value = _status(
            _wet(id=0, dry_time=720, dry_countdown_stalled=True),
            _wet(id=1, dry_time=480, dry_countdown_stalled=True),
        )

        await auto._check_auto_drying(_settings_db(), [], set())

        auto.pm.send_drying_command.assert_called_once_with(1, 0, 0, 0, mode=0)
