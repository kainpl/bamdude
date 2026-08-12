"""Reading a window of history back, in the shape ams-history already uses."""

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient


def _bucket_start(hours_ago: float, bucket_seconds: int = 300) -> datetime:
    """A moment that is exactly the start of a bucket.

    Readings placed a few seconds apart from an arbitrary base fall into ONE
    bucket almost always and into two when the base happens to land within those
    few seconds of a boundary — a test that fails a few times in a hundred, and
    which was seen failing. Aligning removes the coincidence rather than hiding
    it.
    """
    moment = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    epoch = moment.timestamp()
    return datetime.fromtimestamp(epoch - (epoch % bucket_seconds), tz=timezone.utc)


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
        now = _bucket_start(1)
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

        now = _bucket_start(1)
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


async def _sensor_with(db_session, readings, kind="temperature"):
    """A sensor plus its recorded readings. `readings` is (seconds_ago, value).

    `seconds_ago` counts back from a bucket start, so a multiple of the bucket
    width lands on a boundary and anything smaller lands inside the bucket
    above it.
    """
    from backend.app.models.smart_sensor import SmartSensor
    from backend.app.models.smart_sensor_history import SmartSensorHistory
    from backend.app.models.zigbee_device import ZigbeeDevice

    db_session.add(ZigbeeDevice(ieee="cc:dd", kind="sensor", name="SONOFF"))
    sensor = SmartSensor(name="Workshop", zigbee_ieee="cc:dd")
    db_session.add(sensor)
    await db_session.commit()
    await db_session.refresh(sensor)

    now = _bucket_start(0)
    for seconds_ago, value in readings:
        db_session.add(
            SmartSensorHistory(
                sensor_id=sensor.id,
                sensor_kind=kind,
                value=value,
                recorded_at=now - timedelta(seconds=seconds_ago),
            )
        )
    await db_session.commit()
    return sensor


class TestSensorBucketing:
    """One sensor writes some fifty temperature and a hundred humidity readings
    an hour. A week is seventeen thousand points; the chart gets buckets."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_readings_in_one_bucket_collapse_to_their_average(self, async_client: AsyncClient, db_session):
        # Three readings seconds apart land in one five-minute bucket at a
        # 24-hour window. Counting BACK from a bucket start, so 3600 is the
        # boundary and the other two sit just inside the bucket above it.
        sensor = await _sensor_with(db_session, [(3600, 20.0), (3595, 22.0), (3590, 24.0)])

        body = (await async_client.get(f"/api/v1/zigbee/sensors/{sensor.id}/history?kind=temperature&hours=24")).json()

        assert [p["value"] for p in body["points"]] == [22.0]
        assert body["bucket_seconds"] == 300

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_an_empty_stretch_is_omitted_not_sent_as_null(self, async_client: AsyncClient, db_session):
        """The plug endpoint fills its gaps with null so the line breaks where
        nothing was consumed. A sensor is silent on a schedule, not for want of
        an event: breaking the line every minute would claim gaps that did not
        happen. Tidying the two endpoints into one shape reintroduces them."""
        sensor = await _sensor_with(db_session, [(7200, 20.0), (300, 24.0)])

        points = (
            await async_client.get(f"/api/v1/zigbee/sensors/{sensor.id}/history?kind=temperature&hours=24")
        ).json()["points"]

        assert len(points) == 2
        assert all(p["value"] is not None for p in points)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_statistics_come_from_the_readings_not_the_buckets(self, async_client: AsyncClient, db_session):
        """Averaging into buckets first loses the peak, and the peak is the one
        number the smoothed line cannot show."""
        sensor = await _sensor_with(db_session, [(3600, 20.0), (3595, 22.0), (3590, 60.0)])

        body = (await async_client.get(f"/api/v1/zigbee/sensors/{sensor.id}/history?kind=temperature&hours=24")).json()

        assert [p["value"] for p in body["points"]] == [34.0]
        assert body["max_value"] == 60.0
        assert body["min_value"] == 20.0
        assert body["avg_value"] == 34.0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_bucket_table_is_the_one_the_plug_endpoint_uses(self, async_client: AsyncClient, db_session):
        sensor = await _sensor_with(db_session, [(600, 20.0)])

        for hours, seconds in ((6, 60), (24, 300), (48, 600), (168, 1800)):
            body = (
                await async_client.get(f"/api/v1/zigbee/sensors/{sensor.id}/history?kind=temperature&hours={hours}")
            ).json()
            assert body["bucket_seconds"] == seconds, hours

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_an_unknown_quantity_is_a_400_not_an_empty_series(self, async_client: AsyncClient, db_session):
        """Matching nothing would read as "the sensor has not recorded anything
        yet", which is a different fact and hides the caller's bug."""
        sensor = await _sensor_with(db_session, [(600, 20.0)])

        rsp = await async_client.get(f"/api/v1/zigbee/sensors/{sensor.id}/history?kind=loudness")

        assert rsp.status_code == 400

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_sensor_with_no_history_is_an_empty_list_not_an_error(self, async_client: AsyncClient, db_session):
        sensor = await _sensor_with(db_session, [])

        body = (await async_client.get(f"/api/v1/zigbee/sensors/{sensor.id}/history?kind=temperature")).json()

        assert body["points"] == []
        assert body["min_value"] is None
        assert body["avg_value"] is None
        assert body["max_value"] is None
