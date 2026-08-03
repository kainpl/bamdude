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

    assert [point["power"] for point in body["points"] if point["power"] is not None] == [10.0]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_wider_window_reaches_further_back(async_client: AsyncClient, db_session):
    plug = await _plug_with_rows(db_session)

    body = (await async_client.get(f"/api/v1/smart-plugs/{plug.id}/power-history?hours=168")).json()

    measured = sorted(point["power"] for point in body["points"] if point["power"] is not None)
    assert measured == [10.0, 20.0]


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


class TestBucketing:
    """Half a million points is what thirty days of five-second readings comes
    to for one plug. The chart gets buckets, and has to be told they are."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_readings_in_one_bucket_collapse_to_their_average(self, async_client: AsyncClient, db_session):
        from backend.app.models.smart_plug import SmartPlug
        from backend.app.models.smart_plug_power_history import SmartPlugPowerHistory

        plug = SmartPlug(name="P1", plug_type="tasmota", ip_address="10.0.0.9", enabled=True)
        db_session.add(plug)
        await db_session.commit()
        await db_session.refresh(plug)

        # Three readings a few seconds apart: one five-minute bucket at a
        # 24-hour window.
        now = datetime.now(timezone.utc) - timedelta(hours=1)
        for offset, power in ((0, 10.0), (5, 20.0), (10, 60.0)):
            db_session.add(
                SmartPlugPowerHistory(plug_id=plug.id, power=power, recorded_at=now + timedelta(seconds=offset))
            )
        await db_session.commit()

        body = (await async_client.get(f"/api/v1/smart-plugs/{plug.id}/power-history?hours=24")).json()

        assert [p["power"] for p in body["points"]] == [30.0]
        assert body["bucket_seconds"] == 300

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_gap_inside_the_span_is_a_null_not_a_missing_point(self, async_client: AsyncClient, db_session):
        """A flat line and a lost connection must not look alike. Omitting the
        point lets the chart join the two sides with a straight line and draw
        consumption that never happened."""
        from backend.app.models.smart_plug import SmartPlug
        from backend.app.models.smart_plug_power_history import SmartPlugPowerHistory

        plug = SmartPlug(name="P1", plug_type="tasmota", ip_address="10.0.0.9", enabled=True)
        db_session.add(plug)
        await db_session.commit()
        await db_session.refresh(plug)

        now = datetime.now(timezone.utc)
        db_session.add(SmartPlugPowerHistory(plug_id=plug.id, power=10.0, recorded_at=now - timedelta(hours=2)))
        db_session.add(SmartPlugPowerHistory(plug_id=plug.id, power=20.0, recorded_at=now - timedelta(minutes=5)))
        await db_session.commit()

        points = (await async_client.get(f"/api/v1/smart-plugs/{plug.id}/power-history?hours=24")).json()["points"]

        assert points[0]["power"] == 10.0
        assert points[-1]["power"] == 20.0
        assert any(p["power"] is None for p in points), "the two hours of silence have to be visible"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_bucket_widens_with_the_window(self, async_client: AsyncClient, db_session):
        plug = await _plug_with_rows(db_session)

        widths = {}
        for hours in (6, 24, 48, 168):
            body = (await async_client.get(f"/api/v1/smart-plugs/{plug.id}/power-history?hours={hours}")).json()
            widths[hours] = body["bucket_seconds"]

        assert widths == {6: 60, 24: 300, 48: 600, 168: 1800}

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_statistics_keep_the_peak_the_average_hides(self, async_client: AsyncClient, db_session):
        """Measured on the farm: a five-minute bucket spanning a switch-on
        averaged 66 W over readings running from 3 W to 174 W. The line smooths
        that; the statistics are where the peak survives."""
        from backend.app.models.smart_plug import SmartPlug
        from backend.app.models.smart_plug_power_history import SmartPlugPowerHistory

        plug = SmartPlug(name="P1", plug_type="tasmota", ip_address="10.0.0.9", enabled=True)
        db_session.add(plug)
        await db_session.commit()
        await db_session.refresh(plug)

        now = datetime.now(timezone.utc) - timedelta(hours=1)
        for offset, power in ((0, 3.0), (5, 174.0), (10, 21.0)):
            db_session.add(
                SmartPlugPowerHistory(plug_id=plug.id, power=power, recorded_at=now + timedelta(seconds=offset))
            )
        await db_session.commit()

        body = (await async_client.get(f"/api/v1/smart-plugs/{plug.id}/power-history?hours=24")).json()

        assert body["max_power"] == 174.0
        assert body["min_power"] == 3.0
        assert body["avg_power"] == 66.0
        assert [p["power"] for p in body["points"]] == [66.0], "the line shows the average"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_no_history_is_still_an_empty_list(self, async_client: AsyncClient, db_session):
        """Buckets are filled between the first reading and the last. With no
        readings there is no span, and a farm set up this morning must not get
        288 nulls."""
        from backend.app.models.smart_plug import SmartPlug

        plug = SmartPlug(name="P2", plug_type="tasmota", ip_address="10.0.0.8", enabled=True)
        db_session.add(plug)
        await db_session.commit()
        await db_session.refresh(plug)

        body = (await async_client.get(f"/api/v1/smart-plugs/{plug.id}/power-history")).json()

        assert body["points"] == []
        assert body["max_power"] is None
