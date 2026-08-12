"""What the new rows mean once they exist."""

import pytest
from sqlalchemy import select


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_sensor_may_hold_one_threshold_per_quantity(db_session):
    """Two limits on the same quantity would make "the" state ambiguous."""
    from sqlalchemy.exc import IntegrityError

    from backend.app.models.smart_sensor import SmartSensor
    from backend.app.models.smart_sensor_threshold import SmartSensorThreshold
    from backend.app.models.zigbee_device import ZigbeeDevice

    db_session.add(ZigbeeDevice(ieee="aa:bb", kind="sensor", name="SONOFF"))
    sensor = SmartSensor(name="Workshop", zigbee_ieee="aa:bb")
    db_session.add(sensor)
    await db_session.commit()
    await db_session.refresh(sensor)

    db_session.add(SmartSensorThreshold(sensor_id=sensor.id, kind="temperature", max_value=30.0))
    await db_session.commit()

    db_session.add(SmartSensorThreshold(sensor_id=sensor.id, kind="temperature", max_value=40.0))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_new_threshold_starts_clear(db_session):
    """A limit that has never been evaluated has not been breached."""
    from backend.app.models.smart_sensor import SmartSensor
    from backend.app.models.smart_sensor_threshold import SmartSensorThreshold
    from backend.app.models.zigbee_device import ZigbeeDevice

    db_session.add(ZigbeeDevice(ieee="cc:dd", kind="sensor", name="SONOFF"))
    sensor = SmartSensor(name="Workshop", zigbee_ieee="cc:dd")
    db_session.add(sensor)
    await db_session.commit()
    await db_session.refresh(sensor)

    row = SmartSensorThreshold(sensor_id=sensor.id, kind="humidity", max_value=60.0)
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)

    assert row.state == "ok"
    assert row.enabled is True
    assert row.notified_at is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_sensor_carries_its_own_silence(db_session):
    """Silence has no quantity — the device is quiet, not the temperature — so
    it cannot live in a per-quantity row."""
    from backend.app.models.smart_sensor import SmartSensor

    assert {"silent_since", "silence_notified_at"} <= set(SmartSensor.__table__.columns.keys())


@pytest.mark.asyncio
@pytest.mark.integration
async def test_providers_gained_the_two_toggles(db_session):
    from backend.app.models.notification import NotificationProvider

    assert {"on_sensor_threshold", "on_sensor_silent"} <= set(NotificationProvider.__table__.columns.keys())


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_five_templates_are_seeded(test_engine, db_session):
    """Without a row here the send falls back to a bare event name.

    The seed is invoked explicitly: the test database is built by
    ``create_all`` and does not replay migration seeds, so asserting on the
    rows alone would be asserting on the harness rather than on m127.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from backend.app.migrations import m127_sensor_thresholds as m
    from backend.app.models.notification_template import NotificationTemplate

    await m.seed(async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False))

    wanted = {
        "sensor_above_max",
        "sensor_below_min",
        "sensor_back_in_range",
        "sensor_silent",
        "sensor_speaking_again",
    }
    rows = await db_session.execute(
        select(NotificationTemplate.event_type).where(NotificationTemplate.event_type.in_(wanted))
    )
    assert {row[0] for row in rows.all()} == wanted


@pytest.mark.asyncio
@pytest.mark.integration
async def test_seeding_twice_does_not_duplicate(test_engine, db_session):
    """DEBUG=true re-runs the newest migration on every start."""
    from sqlalchemy import func
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from backend.app.migrations import m127_sensor_thresholds as m
    from backend.app.models.notification_template import NotificationTemplate

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    await m.seed(factory)
    await m.seed(factory)

    count = await db_session.execute(
        select(func.count(NotificationTemplate.id)).where(NotificationTemplate.event_type == "sensor_silent")
    )
    assert count.scalar_one() == 1
