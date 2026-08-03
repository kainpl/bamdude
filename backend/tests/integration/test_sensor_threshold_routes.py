"""Reading and writing thresholds.

The whole set goes at once, like the reporting dialog: a quantity absent from
the body ends up with no threshold.
"""

import pytest
from httpx import AsyncClient


async def _sensor(db_session):
    from backend.app.models.smart_sensor import SmartSensor
    from backend.app.models.zigbee_device import ZigbeeDevice

    db_session.add(ZigbeeDevice(ieee="aa:bb", kind="sensor", name="SONOFF"))
    sensor = SmartSensor(name="Workshop", zigbee_ieee="aa:bb")
    db_session.add(sensor)
    await db_session.commit()
    await db_session.refresh(sensor)
    return sensor


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_sensor_with_no_thresholds_answers_with_an_empty_list(async_client: AsyncClient, db_session):
    sensor = await _sensor(db_session)

    body = (await async_client.get(f"/api/v1/zigbee/sensors/{sensor.id}/thresholds")).json()

    assert body["thresholds"] == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_whole_set_is_written_at_once(async_client: AsyncClient, db_session):
    sensor = await _sensor(db_session)

    rsp = await async_client.put(
        f"/api/v1/zigbee/sensors/{sensor.id}/thresholds",
        json={
            "thresholds": [
                {"kind": "temperature", "max_value": 30.0, "deadband": 1.0},
                {"kind": "humidity", "max_value": 60.0, "deadband": 2.0},
            ]
        },
    )

    assert rsp.status_code == 200, rsp.text
    assert {t["kind"] for t in rsp.json()["thresholds"]} == {"temperature", "humidity"}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_quantity_left_out_loses_its_threshold(async_client: AsyncClient, db_session):
    sensor = await _sensor(db_session)
    await async_client.put(
        f"/api/v1/zigbee/sensors/{sensor.id}/thresholds",
        json={"thresholds": [{"kind": "temperature", "max_value": 30.0}, {"kind": "humidity", "max_value": 60.0}]},
    )

    await async_client.put(
        f"/api/v1/zigbee/sensors/{sensor.id}/thresholds",
        json={"thresholds": [{"kind": "temperature", "max_value": 30.0}]},
    )

    body = (await async_client.get(f"/api/v1/zigbee/sensors/{sensor.id}/thresholds")).json()
    assert [t["kind"] for t in body["thresholds"]] == ["temperature"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_editing_a_limit_keeps_the_alarm_state(async_client: AsyncClient, db_session):
    """Rewriting a limit is not an acknowledgement. The next evaluation decides,
    and if the reading is now inside the new limit it says so honestly."""
    from sqlalchemy import select, update

    from backend.app.models.smart_sensor_threshold import SmartSensorThreshold

    sensor = await _sensor(db_session)
    await async_client.put(
        f"/api/v1/zigbee/sensors/{sensor.id}/thresholds",
        json={"thresholds": [{"kind": "temperature", "max_value": 30.0}]},
    )
    await db_session.execute(update(SmartSensorThreshold).values(state="above"))
    await db_session.commit()

    await async_client.put(
        f"/api/v1/zigbee/sensors/{sensor.id}/thresholds",
        json={"thresholds": [{"kind": "temperature", "max_value": 40.0}]},
    )

    row = (await db_session.execute(select(SmartSensorThreshold.state, SmartSensorThreshold.max_value))).one()
    assert row.state == "above"
    assert row.max_value == 40.0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_row_with_neither_limit_is_refused(async_client: AsyncClient, db_session):
    """An empty demand. Stored, it would be a row that can never fire and can
    never be explained."""
    sensor = await _sensor(db_session)

    rsp = await async_client.put(
        f"/api/v1/zigbee/sensors/{sensor.id}/thresholds",
        json={"thresholds": [{"kind": "temperature", "deadband": 1.0}]},
    )

    assert rsp.status_code == 422


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_unknown_quantity_is_refused(async_client: AsyncClient, db_session):
    sensor = await _sensor(db_session)

    rsp = await async_client.put(
        f"/api/v1/zigbee/sensors/{sensor.id}/thresholds",
        json={"thresholds": [{"kind": "loudness", "max_value": 80.0}]},
    )

    assert rsp.status_code == 422


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_reading_unit_rides_along(async_client: AsyncClient, db_session):
    """The dialog labels each field with it; a second key→unit map on the
    frontend would be a copy of the registry."""
    sensor = await _sensor(db_session)

    rsp = await async_client.put(
        f"/api/v1/zigbee/sensors/{sensor.id}/thresholds",
        json={"thresholds": [{"kind": "temperature", "max_value": 30.0}]},
    )

    assert rsp.json()["thresholds"][0]["unit"] == "°C"
    assert rsp.json()["thresholds"][0]["state"] == "ok"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_unknown_sensor_is_a_404(async_client: AsyncClient):
    assert (await async_client.get("/api/v1/zigbee/sensors/999/thresholds")).status_code == 404
    rsp = await async_client.put("/api/v1/zigbee/sensors/999/thresholds", json={"thresholds": []})
    assert rsp.status_code == 404
