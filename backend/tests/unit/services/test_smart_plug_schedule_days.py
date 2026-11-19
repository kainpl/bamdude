"""Daily scheduling and manual control share a device, not execution history."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.models.smart_plug import SmartPlug
from backend.app.services.smart_plug_manager import SmartPlugManager


@pytest.fixture
def schedule(monkeypatch):
    from backend.app.api.routes import smart_plugs as routes
    from backend.app.core import database
    from backend.app.schemas.smart_plug import SmartPlugControl
    from backend.app.services import smart_plug_manager as module

    manager = SmartPlugManager()
    # A complete row: the control route refuses a Tasmota plug saved without its address.
    plug = SmartPlug(
        id=1, name="Schedule test", plug_type="tasmota", ip_address="192.168.1.50", enabled=True, schedule_enabled=True
    )
    plug.schedule_on_time = None
    plug.schedule_off_time = None
    plug.last_state = "OFF"
    plugs = [plug]
    events = []
    clock = SimpleNamespace(now=datetime(2026, 10, 13, 7, 30))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now if tz is None else clock.now.replace(tzinfo=tz)

    async def command(target, action):
        events.append((target.id, action))
        target.last_state = action.upper()
        return True

    service = SimpleNamespace(
        turn_on=AsyncMock(),
        turn_off=AsyncMock(),
        toggle=AsyncMock(),
    )

    async def on(target):
        return await command(target, "on")

    async def off(target):
        return await command(target, "off")

    async def toggle(target):
        return await command(target, "off" if target.last_state == "ON" else "on")

    service.turn_on.side_effect = on
    service.turn_off.side_effect = off
    service.toggle.side_effect = toggle
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.side_effect = lambda: plugs
    result.scalar_one_or_none.return_value = plug
    db.execute.return_value = result
    session = MagicMock()
    session.return_value.__aenter__ = AsyncMock(return_value=db)
    session.return_value.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(database, "async_session", session)
    monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(manager, "get_service_for_plug", AsyncMock(return_value=service))
    monkeypatch.setattr(routes, "_get_service_for_plug", AsyncMock(return_value=service))
    monkeypatch.setattr("backend.app.services.mqtt_relay.mqtt_relay.on_smart_plug_state", AsyncMock())

    async def tick(at):
        clock.now = at
        await manager._check_schedules()

    async def manual(action):
        # Exercise the actual control route, including its state update and commit.
        response = await routes.control_smart_plug(plug.id, SmartPlugControl(action=action), db, None)
        assert response["success"]

    return SimpleNamespace(plug=plug, plugs=plugs, events=events, tick=tick, manual=manual, service=service)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["on", "off"])
@pytest.mark.parametrize("manual", [None, "on", "off", "toggle"])
@pytest.mark.parametrize(
    "first", [datetime(2026, 10, 13, 7, 30), datetime(2026, 10, 31, 7, 30), datetime(2026, 12, 31, 7, 30)]
)
async def test_daily_action_after_manual_control_and_calendar_rollover(schedule, action, manual, first):
    setattr(schedule.plug, f"schedule_{action}_time", "07:30")
    for day in range(3):
        at = first + timedelta(days=day)
        before = len(schedule.events)
        await schedule.tick(at)
        assert schedule.events[before:] == [(1, action)], "Every calendar day needs its own scheduled command"
        if manual:
            await schedule.manual(manual)
        state = schedule.plug.last_state
        before = len(schedule.events)
        await schedule.tick(at + timedelta(seconds=30))
        await schedule.tick(at.replace(hour=12))
        assert len(schedule.events) == before, "Do not undo a manual action or repeat a successful minute"
        assert schedule.plug.last_state == state


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", [None, "on", "off", "toggle"])
async def test_both_schedules_with_manual_actions_between_them(schedule, manual):
    schedule.plug.schedule_on_time = "07:30"
    schedule.plug.schedule_off_time = "23:00"
    for day in (13, 14, 15):
        for hour, minute, action in ((7, 30, "on"), (23, 0, "off")):
            at = datetime(2026, 10, day, hour, minute)
            if manual:
                await schedule.manual(manual)
            before = len(schedule.events)
            await schedule.tick(at)
            assert schedule.events[before:] == [(1, action)]
            if manual:
                await schedule.manual(manual)
            before = len(schedule.events)
            await schedule.tick(at + timedelta(seconds=30))
            assert len(schedule.events) == before


@pytest.mark.asyncio
async def test_identical_on_off_times_do_not_replay_on_every_poll(schedule):
    schedule.plug.schedule_on_time = schedule.plug.schedule_off_time = "07:30"
    for day in (13, 14):
        before = len(schedule.events)
        await schedule.tick(datetime(2026, 10, day, 7, 30))
        await schedule.tick(datetime(2026, 10, day, 7, 30, 30))
        assert schedule.events[before:] == [(1, "on"), (1, "off")]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["on", "off"])
async def test_failed_command_can_retry_in_same_minute_and_next_day(schedule, action):
    setattr(schedule.plug, f"schedule_{action}_time", "07:30")
    command = getattr(schedule.service, f"turn_{action}")
    command.side_effect = [False, True, True]
    for seconds, expected in ((0, 1), (20, 2), (40, 2)):
        await schedule.tick(datetime(2026, 10, 13, 7, 30, seconds))
        assert command.await_count == expected
    await schedule.tick(datetime(2026, 10, 14, 7, 30))
    assert command.await_count == 3


@pytest.mark.asyncio
async def test_plugs_with_equal_times_execute_independently(schedule):
    schedule.plug.schedule_on_time = "07:30"
    schedule.plugs.append(SmartPlug(id=2, name="Second", schedule_on_time="07:30"))
    for day in (13, 14):
        before = len(schedule.events)
        await schedule.tick(datetime(2026, 10, day, 7, 30))
        await schedule.tick(datetime(2026, 10, day, 7, 30, 30))
        assert schedule.events[before:] == [(1, "on"), (2, "on")]


@pytest.mark.asyncio
async def test_manual_command_before_due_time_does_not_consume_schedule(schedule):
    schedule.plug.schedule_on_time = "07:30"
    await schedule.manual("on")
    await schedule.tick(datetime(2026, 10, 13, 7, 29))
    assert schedule.events == [(1, "on")]
    await schedule.tick(datetime(2026, 10, 13, 7, 30))
    assert schedule.events == [(1, "on"), (1, "on")]
