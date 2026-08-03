"""Reading a window of history back, in the shape ams-history already uses."""

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient


async def _plug_with_rows(db_session):
    from backend.app.models.smart_plug import SmartPlug
    from backend.app.models.smart_plug_power_history import SmartPlugPowerHistory

    plug = SmartPlug(name="P1", plug_type="tasmota", ip_address="10.0.0.9", enabled=True)
    db_session.add(plug)
    await db_session.commit()
    await db_session.refresh(plug)

    now = datetime.now(timezone.utc)
    db_session.add(SmartPlugPowerHistory(plug_id=plug.id, power=10.0, recorded_at=now - timedelta(hours=1)))
    db_session.add(SmartPlugPowerHistory(plug_id=plug.id, power=20.0, recorded_at=now - timedelta(days=3)))
    await db_session.commit()
    return plug


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_window_returns_what_falls_inside_it(async_client: AsyncClient, db_session):
    plug = await _plug_with_rows(db_session)

    body = (await async_client.get(f"/api/v1/smart-plugs/{plug.id}/power-history?hours=24")).json()

    assert [point["power"] for point in body["points"]] == [10.0]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_wider_window_reaches_further_back(async_client: AsyncClient, db_session):
    plug = await _plug_with_rows(db_session)

    body = (await async_client.get(f"/api/v1/smart-plugs/{plug.id}/power-history?hours=168")).json()

    assert sorted(point["power"] for point in body["points"]) == [10.0, 20.0]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_points_come_back_oldest_first(async_client: AsyncClient, db_session):
    """A chart draws them in order. Unordered, the line zig-zags across the plot
    and looks like the data is wrong rather than the query."""
    plug = await _plug_with_rows(db_session)

    body = (await async_client.get(f"/api/v1/smart-plugs/{plug.id}/power-history?hours=168")).json()

    stamps = [point["recorded_at"] for point in body["points"]]
    assert stamps == sorted(stamps)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_plug_with_no_history_is_an_empty_list_not_an_error(async_client: AsyncClient, db_session):
    """A farm set up this morning has no history, and that is not a fault."""
    from backend.app.models.smart_plug import SmartPlug

    plug = SmartPlug(name="P2", plug_type="tasmota", ip_address="10.0.0.8", enabled=True)
    db_session.add(plug)
    await db_session.commit()
    await db_session.refresh(plug)

    rsp = await async_client.get(f"/api/v1/smart-plugs/{plug.id}/power-history")

    assert rsp.status_code == 200
    assert rsp.json()["points"] == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_unknown_plug_is_a_404(async_client: AsyncClient):
    assert (await async_client.get("/api/v1/smart-plugs/999/power-history")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_window_is_bounded(async_client: AsyncClient, db_session):
    """Thirty days of five-second readings is half a million points for one
    plug — too many to draw and too many to hand over."""
    plug = await _plug_with_rows(db_session)

    assert (await async_client.get(f"/api/v1/smart-plugs/{plug.id}/power-history?hours=1000")).status_code == 422


@pytest.mark.asyncio
@pytest.mark.integration
async def test_sensor_history_is_asked_for_one_quantity_at_a_time(async_client: AsyncClient, db_session):
    from backend.app.models.smart_sensor import SmartSensor
    from backend.app.models.smart_sensor_history import SmartSensorHistory
    from backend.app.models.zigbee_device import ZigbeeDevice

    db_session.add(ZigbeeDevice(ieee="aa:bb", kind="sensor", name="SONOFF"))
    sensor = SmartSensor(name="Workshop", zigbee_ieee="aa:bb")
    db_session.add(sensor)
    await db_session.commit()
    await db_session.refresh(sensor)

    now = datetime.now(timezone.utc)
    db_session.add(SmartSensorHistory(sensor_id=sensor.id, sensor_kind="temperature", value=23.4, recorded_at=now))
    db_session.add(SmartSensorHistory(sensor_id=sensor.id, sensor_kind="humidity", value=41.0, recorded_at=now))
    await db_session.commit()

    body = (await async_client.get(f"/api/v1/zigbee/sensors/{sensor.id}/history?kind=temperature")).json()

    assert [point["value"] for point in body["points"]] == [23.4]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_unknown_sensor_is_a_404(async_client: AsyncClient):
    assert (await async_client.get("/api/v1/zigbee/sensors/999/history?kind=temperature")).status_code == 404
