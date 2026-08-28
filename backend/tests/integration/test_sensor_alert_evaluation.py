"""What the sweeps decide, against a real database.

The two facts worth guarding here are both about restarts: state is read from
the row, not from process memory, and the silence sweep does nothing until the
process has been up longer than the window it is judging.
"""

from datetime import datetime, timedelta, timezone

import pytest


async def _sensor(db_session, ieee="aa:bb", name="Workshop", printer_id=None):
    from backend.app.models.smart_sensor import SmartSensor
    from backend.app.models.zigbee_device import ZigbeeDevice

    db_session.add(ZigbeeDevice(ieee=ieee, kind="sensor", name="SONOFF"))
    sensor = SmartSensor(name=name, zigbee_ieee=ieee, printer_id=printer_id)
    db_session.add(sensor)
    await db_session.commit()
    await db_session.refresh(sensor)
    return sensor


async def _reading(db_session, sensor, kind, value, seconds_ago=0):
    from backend.app.models.smart_sensor_history import SmartSensorHistory

    db_session.add(
        SmartSensorHistory(
            sensor_id=sensor.id,
            sensor_kind=kind,
            value=value,
            recorded_at=datetime.now(timezone.utc) - timedelta(seconds=seconds_ago),
        )
    )
    await db_session.commit()


async def _threshold(db_session, sensor, kind="temperature", **kwargs):
    from backend.app.models.smart_sensor_threshold import SmartSensorThreshold

    row = SmartSensorThreshold(sensor_id=sensor.id, kind=kind, **kwargs)
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_reading_past_the_limit_raises_once(db_session):
    from backend.app.services.sensor_alerts import evaluate_thresholds

    sensor = await _sensor(db_session)
    await _threshold(db_session, sensor, max_value=30.0, deadband=1.0)
    await _reading(db_session, sensor, "temperature", 31.2)

    first = await evaluate_thresholds(db_session)
    assert [e.template for e in first] == ["sensor_above_max"]

    # Nothing new happened; the same reading must not ring again.
    second = await evaluate_thresholds(db_session)
    assert second == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_state_is_read_from_the_row_not_from_memory(db_session):
    """This is the _ams_alarm_cooldown fault, restated as an assertion: that
    dictionary lives in the process and forgets on every restart."""
    from sqlalchemy import select

    from backend.app.models.smart_sensor_threshold import SmartSensorThreshold
    from backend.app.services.sensor_alerts import ABOVE, evaluate_thresholds

    sensor = await _sensor(db_session)
    row = await _threshold(db_session, sensor, max_value=30.0, deadband=1.0)
    await _reading(db_session, sensor, "temperature", 31.2)

    await evaluate_thresholds(db_session)

    # Read the COLUMNS back, not the mapped object: the identity map would hand
    # back the same instance the evaluation just mutated, and the assertion
    # would pass whether or not anything was ever written.
    stored = (
        await db_session.execute(
            select(SmartSensorThreshold.state, SmartSensorThreshold.notified_at).where(
                SmartSensorThreshold.id == row.id
            )
        )
    ).one()
    assert stored.state == ABOVE
    assert stored.notified_at is not None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_it_clears_only_past_the_deadband(db_session):
    from backend.app.services.sensor_alerts import evaluate_thresholds

    sensor = await _sensor(db_session)
    await _threshold(db_session, sensor, max_value=30.0, deadband=1.0)
    await _reading(db_session, sensor, "temperature", 31.2)
    await evaluate_thresholds(db_session)

    # Back under the limit, but not by enough.
    await _reading(db_session, sensor, "temperature", 29.5)
    assert await evaluate_thresholds(db_session) == []

    await _reading(db_session, sensor, "temperature", 28.4)
    events = await evaluate_thresholds(db_session)
    assert [e.template for e in events] == ["sensor_back_in_range"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_disabling_a_threshold_clears_it_silently(db_session):
    """An all-clear about a limit somebody just switched off is a message
    about nothing."""
    from sqlalchemy import select

    from backend.app.models.smart_sensor_threshold import SmartSensorThreshold
    from backend.app.services.sensor_alerts import OK, evaluate_thresholds

    sensor = await _sensor(db_session)
    row = await _threshold(db_session, sensor, max_value=30.0, deadband=1.0)
    await _reading(db_session, sensor, "temperature", 31.2)
    await evaluate_thresholds(db_session)

    row.enabled = False
    await db_session.commit()

    assert await evaluate_thresholds(db_session) == []
    state = (
        await db_session.execute(select(SmartSensorThreshold.state).where(SmartSensorThreshold.id == row.id))
    ).scalar_one()
    assert state == OK


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_quantity_with_no_reading_is_not_evaluated(db_session):
    """Configuring a limit before the first report is allowed."""
    from backend.app.services.sensor_alerts import evaluate_thresholds

    sensor = await _sensor(db_session)
    await _threshold(db_session, sensor, kind="co2", max_value=1000.0)

    assert await evaluate_thresholds(db_session) == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_message_carries_the_place_and_the_numbers(db_session):
    from backend.app.services.sensor_alerts import evaluate_thresholds

    sensor = await _sensor(db_session)
    await _threshold(db_session, sensor, max_value=30.0)
    await _reading(db_session, sensor, "temperature", 31.2)

    [event] = await evaluate_thresholds(db_session)
    assert event.variables["value"] == "31.2"
    assert event.variables["limit"] == "30"
    assert event.variables["unit"] == "°C"
    # No location on this sensor, so the place falls back to its own name — a
    # message opening with an empty dash says nothing about where to walk.
    assert event.variables["location"] == "Workshop"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_printer_bound_sensor_names_the_printer_it_is_taped_to(db_session):
    """⚠️ The printer, not the room it stands in.

    A sensor bound to a printer has no location — the two bindings are
    exclusive — so without this the message would fall back to the sensor's own
    name and an alert about an enclosure would not say which machine.
    """
    from backend.app.models.printer import Printer
    from backend.app.services.sensor_alerts import evaluate_thresholds

    printer = Printer(name="X1C #2", ip_address="192.168.1.9", access_code="12345678", serial_number="SN9")
    db_session.add(printer)
    await db_session.commit()
    await db_session.refresh(printer)

    sensor = await _sensor(db_session, name="Enclosure", printer_id=printer.id)
    await _threshold(db_session, sensor, max_value=30.0)
    await _reading(db_session, sensor, "temperature", 31.2)

    [event] = await evaluate_thresholds(db_session)
    assert event.variables["location"] == "X1C #2"


class TestSilence:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_sensor_past_its_window_is_reported(self, db_session):
        from sqlalchemy import select

        from backend.app.models.smart_sensor import SmartSensor
        from backend.app.services.sensor_alerts import sweep_silence

        sensor = await _sensor(db_session)
        # Default max_interval is 900 s and the stale multiplier is 2, so the
        # window is half an hour.
        await _reading(db_session, sensor, "temperature", 22.0, seconds_ago=4000)

        events = await sweep_silence(db_session, uptime_seconds=10_000)
        assert [e.template for e in events] == ["sensor_silent"]

        stored = (
            await db_session.execute(select(SmartSensor.silent_since).where(SmartSensor.id == sensor.id))
        ).scalar_one()
        assert stored is not None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_it_does_nothing_while_the_process_is_younger_than_the_window(self, db_session):
        """Without this the first restart announces silence for the whole farm:
        every sensor's last reading is older than a process that just booted."""
        from backend.app.services.sensor_alerts import sweep_silence

        sensor = await _sensor(db_session)
        await _reading(db_session, sensor, "temperature", 22.0, seconds_ago=4000)

        assert await sweep_silence(db_session, uptime_seconds=30) == []

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_sensor_that_never_spoke_does_not_alarm(self, db_session):
        """That is "not set up yet", which is a different fact."""
        from backend.app.services.sensor_alerts import sweep_silence

        await _sensor(db_session)

        assert await sweep_silence(db_session, uptime_seconds=10_000) == []

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_fresh_reading_ends_the_silence(self, db_session):
        from backend.app.services.sensor_alerts import sweep_silence

        sensor = await _sensor(db_session)
        await _reading(db_session, sensor, "temperature", 22.0, seconds_ago=4000)
        await sweep_silence(db_session, uptime_seconds=10_000)

        await _reading(db_session, sensor, "temperature", 22.5)
        events = await sweep_silence(db_session, uptime_seconds=10_000)

        assert [e.template for e in events] == ["sensor_speaking_again"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_silent_sensor_is_reported_once(self, db_session):
        from backend.app.services.sensor_alerts import sweep_silence

        sensor = await _sensor(db_session)
        await _reading(db_session, sensor, "temperature", 22.0, seconds_ago=4000)

        await sweep_silence(db_session, uptime_seconds=10_000)
        assert await sweep_silence(db_session, uptime_seconds=10_000) == []
