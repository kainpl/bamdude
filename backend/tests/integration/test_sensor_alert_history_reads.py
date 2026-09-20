"""How the silence sweep reads the history — the shape, not just the answer.

⚠️ It used to ask the database which quantities a sensor has recorded
(``SELECT DISTINCT sensor_kind WHERE sensor_id = ?``) and then, separately, when
it last recorded anything (``ORDER BY recorded_at DESC LIMIT 1``). Both read
EVERY row of that sensor — measured on a live 94 133-row table at 20.9 ms and
12.4 ms, once per sensor per tick, forever, growing with retention.

Neither can be fixed by an index. The shipped index is
``(sensor_id, sensor_kind, recorded_at)``: a DISTINCT on its second column with
the first pinned has no loose index scan in PostgreSQL, and the same index gives
no ordering by time once only ``sensor_id`` is pinned.

So the question is asked the other way round — of the measurement registry,
which is the only writer of ``sensor_kind`` — and both answers now come from one
cheap read. These tests pin that, because a well-meaning "simplification" back
to ``.distinct()`` would be invisible until a farm's history got large.
"""

from datetime import datetime, timedelta, timezone

import pytest


async def _sensor_with(db_session, readings: list[tuple[str, datetime]]):
    from backend.app.models.smart_sensor import SmartSensor
    from backend.app.models.smart_sensor_history import SmartSensorHistory
    from backend.app.models.zigbee_device import ZigbeeDevice

    db_session.add(ZigbeeDevice(ieee="aa:bb", kind="sensor", name="SONOFF"))
    sensor = SmartSensor(name="Workshop", zigbee_ieee="aa:bb")
    db_session.add(sensor)
    await db_session.commit()
    await db_session.refresh(sensor)

    for kind, at in readings:
        db_session.add(SmartSensorHistory(sensor_id=sensor.id, sensor_kind=kind, value=1.0, recorded_at=at))
    await db_session.commit()
    return sensor


@pytest.mark.asyncio
@pytest.mark.integration
async def test_one_row_per_recorded_kind_carrying_its_newest_reading(db_session):
    from backend.app.services import sensor_alerts

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    sensor = await _sensor_with(
        db_session,
        [
            ("temperature", now - timedelta(hours=3)),
            ("temperature", now - timedelta(minutes=1)),
            ("humidity", now - timedelta(minutes=30)),
        ],
    )

    recorded = dict(await sensor_alerts._recorded_kinds(db_session, sensor))

    assert set(recorded) == {"temperature", "humidity"}
    # The NEWEST of each, not the first one found.
    assert recorded["temperature"] > recorded["humidity"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_kind_that_was_never_recorded_is_absent(db_session):
    """Asking the registry must not invent quantities the sensor never sent."""
    from backend.app.services import sensor_alerts

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    sensor = await _sensor_with(db_session, [("battery", now)])

    recorded = await sensor_alerts._recorded_kinds(db_session, sensor)

    assert [kind for kind, _ in recorded] == ["battery"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_sensor_with_no_history_reads_as_never_reported(db_session):
    from backend.app.services import sensor_alerts

    sensor = await _sensor_with(db_session, [])

    assert await sensor_alerts._recorded_kinds(db_session, sensor) == []
    # "not set up yet", which is not the same as "went silent".
    assert await sensor_alerts._silence_window(db_session, sensor, []) is None


def test_the_statement_asks_the_registry_and_never_scans_for_distinct():
    """The regression guard. If this ever compiles back to a DISTINCT over the
    table, the cost silently becomes proportional to how much history is kept."""
    from backend.app.services.sensor_alerts import _newest_per_kind_stmt
    from backend.app.services.zigbee.measurements import BY_KEY

    sql = str(_newest_per_kind_stmt(1).compile(compile_kwargs={"literal_binds": True}))

    assert "DISTINCT" not in sql.upper()
    # One bounded probe per known quantity, all of them named.
    assert sql.upper().count("LIMIT 1") == len(BY_KEY)
    for kind in BY_KEY:
        assert kind in sql


def test_every_quantity_the_writer_can_produce_is_in_the_registry():
    """The correctness premise, stated where it can fail loudly.

    ``zigbee/sensors.py`` buffers a reading under a key it took from the
    registry, so the history cannot hold a quantity the registry does not know.
    If a second writer ever appears, this file's whole approach needs revisiting
    — the sweep would stop seeing that quantity's history.
    """
    import inspect

    from backend.app.services import measurement_history
    from backend.app.services.zigbee import sensors

    producers = [
        line for line in inspect.getsource(sensors).splitlines() if "measurement_buffer.record_sensor(" in line
    ]
    assert producers, "the sensor reading producer moved — re-check the registry premise"
    for line in producers:
        # The kind argument is the registry key the loop is iterating.
        assert "key" in line

    # And the history writer takes the kind straight from the buffered sample.
    assert "sensor_kind=sample.kind" in inspect.getsource(measurement_history)
