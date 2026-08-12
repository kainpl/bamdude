"""The tick that ties evaluation to sending.

The loop it lives in must survive anything this does: an exception there is a
feature that quietly stops working, taking history writing and pruning with it.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select


async def _sensor_over_limit(db_session):
    from backend.app.models.smart_sensor import SmartSensor
    from backend.app.models.smart_sensor_history import SmartSensorHistory
    from backend.app.models.smart_sensor_threshold import SmartSensorThreshold
    from backend.app.models.zigbee_device import ZigbeeDevice

    db_session.add(ZigbeeDevice(ieee="aa:bb", kind="sensor", name="SONOFF"))
    sensor = SmartSensor(name="Workshop", zigbee_ieee="aa:bb")
    db_session.add(sensor)
    await db_session.commit()
    await db_session.refresh(sensor)

    db_session.add(SmartSensorThreshold(sensor_id=sensor.id, kind="temperature", max_value=30.0, deadband=1.0))
    db_session.add(
        SmartSensorHistory(
            sensor_id=sensor.id,
            sensor_kind="temperature",
            value=31.2,
            recorded_at=datetime.now(timezone.utc) - timedelta(seconds=5),
        )
    )
    await db_session.commit()
    return sensor


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_breached_threshold_reaches_the_notifier(db_session):
    from backend.app.services import sensor_alerts

    await _sensor_over_limit(db_session)

    with patch.object(sensor_alerts.notification_service, "on_sensor_alert", new=AsyncMock()) as send:
        sent = await sensor_alerts.run_sensor_alerts(db_session)

    assert sent == 1
    assert send.await_args.args[0].template == "sensor_above_max"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_failing_notifier_does_not_take_the_loop_down(db_session):
    """The same loop writes history and prunes it. One unreachable provider
    must not stop either."""
    from backend.app.services import sensor_alerts

    await _sensor_over_limit(db_session)

    with patch.object(
        sensor_alerts.notification_service,
        "on_sensor_alert",
        new=AsyncMock(side_effect=RuntimeError("ntfy is down")),
    ):
        sent = await sensor_alerts.run_sensor_alerts(db_session)

    assert sent == 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_state_is_kept_even_when_sending_fails(db_session):
    """State is committed before the send. The other order turns a sustained
    outage into an identical message every minute."""
    from backend.app.models.smart_sensor_threshold import SmartSensorThreshold
    from backend.app.services import sensor_alerts

    sensor = await _sensor_over_limit(db_session)

    with patch.object(
        sensor_alerts.notification_service,
        "on_sensor_alert",
        new=AsyncMock(side_effect=RuntimeError("ntfy is down")),
    ):
        await sensor_alerts.run_sensor_alerts(db_session)

    state = (
        await db_session.execute(select(SmartSensorThreshold.state).where(SmartSensorThreshold.sensor_id == sensor.id))
    ).scalar_one()
    assert state == "above"
