"""Smart-plug power alerts: sent at all, checked in the background, once per crossing.

Before this the alert called a method the notification service does not have,
ran only inside the plug-status request (so only while a page was open) and
would have repeated every five minutes while the reading stayed outside
(upstream 47795649a).
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from backend.app.models.smart_plug import SmartPlug
from backend.app.models.smart_plug_power_history import SmartPlugPowerHistory
from backend.app.services import power_alerts
from backend.app.services.power_alerts import HIGH, INSIDE, LOW, side_of


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TestSideOf:
    def test_above_the_high_limit(self):
        assert side_of(250.0, "ON", low=None, high=200.0) == HIGH

    def test_at_the_limit_is_inside(self):
        assert side_of(200.0, "ON", low=5.0, high=200.0) == INSIDE
        assert side_of(5.0, "ON", low=5.0, high=200.0) == INSIDE

    def test_below_the_low_limit_while_on(self):
        assert side_of(1.0, "ON", low=5.0, high=None) == LOW

    def test_a_switched_off_plug_is_never_low(self):
        assert side_of(0.0, "OFF", low=5.0, high=None) == INSIDE

    def test_an_unknown_switch_state_is_never_low(self):
        assert side_of(0.0, None, low=5.0, high=None) == INSIDE

    def test_high_does_not_need_the_switch_state(self):
        assert side_of(250.0, None, low=5.0, high=200.0) == HIGH

    def test_no_limits_no_alert(self):
        assert side_of(9999.0, "ON", low=None, high=None) == INSIDE


@pytest.fixture
def sent(monkeypatch):
    power_alerts.forget_all()
    mock = AsyncMock()
    monkeypatch.setattr(power_alerts.notification_service, "on_printer_error", mock)
    yield mock
    power_alerts.forget_all()


async def plug_with(db, *, low=None, high=None, state="ON", enabled=True, alerts=True):
    plug = SmartPlug(
        name="Bench plug",
        plug_type="tasmota",
        ip_address="192.0.2.10",
        enabled=enabled,
        power_alert_enabled=alerts,
        power_alert_low=low,
        power_alert_high=high,
        last_state=state,
    )
    db.add(plug)
    await db.commit()
    return plug


async def reading(db, plug, power, *, age=timedelta(seconds=5), unusable=None):
    db.add(SmartPlugPowerHistory(plug_id=plug.id, power=power, recorded_at=now() - age, unusable_reason=unusable))
    await db.commit()


@pytest.mark.asyncio
async def test_a_crossing_is_announced_once(db_session, sent):
    plug = await plug_with(db_session, high=200.0)
    await reading(db_session, plug, 250.0)

    assert await power_alerts.run_power_alerts(db_session) == 1
    await reading(db_session, plug, 260.0, age=timedelta(seconds=1))
    assert await power_alerts.run_power_alerts(db_session) == 0
    assert sent.await_count == 1


@pytest.mark.asyncio
async def test_coming_back_inside_arms_the_next_crossing(db_session, sent, monkeypatch):
    monkeypatch.setattr(power_alerts, "COOLDOWN", timedelta(0))
    plug = await plug_with(db_session, high=200.0)
    await reading(db_session, plug, 250.0, age=timedelta(seconds=30))
    await power_alerts.run_power_alerts(db_session)
    await reading(db_session, plug, 100.0, age=timedelta(seconds=20))
    await power_alerts.run_power_alerts(db_session)
    await reading(db_session, plug, 250.0, age=timedelta(seconds=10))

    assert await power_alerts.run_power_alerts(db_session) == 1
    assert sent.await_count == 2


@pytest.mark.asyncio
async def test_the_cooldown_holds_a_second_crossing_and_keeps_it_owed(db_session, sent):
    plug = await plug_with(db_session, high=200.0)
    await reading(db_session, plug, 250.0, age=timedelta(seconds=30))
    await power_alerts.run_power_alerts(db_session)
    await reading(db_session, plug, 100.0, age=timedelta(seconds=20))
    await power_alerts.run_power_alerts(db_session)
    await reading(db_session, plug, 250.0, age=timedelta(seconds=10))

    assert await power_alerts.run_power_alerts(db_session) == 0
    plug.power_alert_last_triggered = now() - timedelta(minutes=10)
    await db_session.commit()
    assert await power_alerts.run_power_alerts(db_session) == 1


@pytest.mark.asyncio
async def test_the_alert_names_the_plug_and_goes_through_the_printer_error_event(db_session, sent):
    plug = await plug_with(db_session, low=5.0)
    await reading(db_session, plug, 1.0)

    await power_alerts.run_power_alerts(db_session)

    args, kwargs = sent.await_args
    assert args[0] is None  # the plug is bound to no printer
    assert args[1] == "Bench plug"
    assert "1.0" in (args[4] if len(args) > 4 else kwargs["error_detail"])


@pytest.mark.asyncio
async def test_a_stale_reading_is_no_reading(db_session, sent):
    plug = await plug_with(db_session, high=200.0)
    await reading(db_session, plug, 250.0, age=power_alerts.FRESH + timedelta(minutes=1))

    assert await power_alerts.run_power_alerts(db_session) == 0


@pytest.mark.asyncio
async def test_an_unusable_reading_is_skipped_for_the_one_before_it(db_session, sent):
    plug = await plug_with(db_session, high=200.0)
    await reading(db_session, plug, 100.0, age=timedelta(seconds=30))
    await reading(db_session, plug, 999.0, age=timedelta(seconds=5), unusable="state_unknown")

    assert await power_alerts.run_power_alerts(db_session) == 0


@pytest.mark.asyncio
async def test_a_plug_without_alerts_is_not_read(db_session, sent):
    plug = await plug_with(db_session, high=200.0, alerts=False)
    await reading(db_session, plug, 250.0)

    assert await power_alerts.run_power_alerts(db_session) == 0


@pytest.mark.asyncio
async def test_a_failed_send_is_tried_again(db_session, sent):
    plug = await plug_with(db_session, high=200.0)
    await reading(db_session, plug, 250.0)
    sent.side_effect = RuntimeError("provider down")

    assert await power_alerts.run_power_alerts(db_session) == 0
    sent.side_effect = None
    assert await power_alerts.run_power_alerts(db_session) == 1


@pytest.mark.asyncio
async def test_the_switch_state_of_the_newest_reading_outranks_the_plug_row(db_session, sent):
    from backend.app.services.measurement_buffer import measurement_buffer

    plug = await plug_with(db_session, low=5.0, state="ON")
    await reading(db_session, plug, 0.0)
    measurement_buffer.record_power(plug.id, 0.0, state="OFF")
    try:
        assert await power_alerts.run_power_alerts(db_session) == 0
    finally:
        measurement_buffer._switch.pop(plug.id, None)


def test_the_status_route_no_longer_evaluates_alerts():
    from backend.app.api.routes import smart_plugs

    assert not hasattr(smart_plugs, "check_power_alerts")


def test_the_history_loop_runs_the_power_alerts():
    import inspect

    from backend.app.services.smart_plug_manager import SmartPlugManager

    assert "run_power_alerts" in inspect.getsource(SmartPlugManager._history_loop)
