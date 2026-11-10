"""Flush, sample and prune — the loop's work, without the loop.

Each of these fails quietly. A chart that is emptier than it should be looks
exactly like a farm that was idle.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select


@pytest.fixture(autouse=True)
def empty_buffer():
    from backend.app.services.measurement_buffer import measurement_buffer

    measurement_buffer.drain()
    yield
    measurement_buffer.drain()


async def _plug(db, name="P1", plug_type="tasmota", ip="10.0.0.9"):
    from backend.app.models.smart_plug import SmartPlug

    plug = SmartPlug(name=name, plug_type=plug_type, ip_address=ip, enabled=True)
    db.add(plug)
    await db.commit()
    await db.refresh(plug)
    return plug


async def _sensor(db, ieee="aa:bb"):
    from backend.app.models.smart_sensor import SmartSensor
    from backend.app.models.zigbee_device import ZigbeeDevice

    db.add(ZigbeeDevice(ieee=ieee, kind="sensor", name="SONOFF"))
    sensor = SmartSensor(name="Workshop", zigbee_ieee=ieee)
    db.add(sensor)
    await db.commit()
    await db.refresh(sensor)
    return sensor


class TestFlush:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_buffered_power_reaches_the_table(self, db_session):
        from backend.app.models.smart_plug_power_history import SmartPlugPowerHistory
        from backend.app.services.measurement_buffer import measurement_buffer
        from backend.app.services.measurement_history import flush_buffered

        plug = await _plug(db_session)
        measurement_buffer.record_power(plug.id, 42.5)

        assert await flush_buffered(db_session) == 1

        rows = (await db_session.execute(select(SmartPlugPowerHistory))).scalars().all()
        assert [r.power for r in rows] == [42.5]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_sensor_reading_is_resolved_from_its_address(self, db_session):
        """The buffer knows an IEEE because that is what a report carries; the
        table keys on the adopted row."""
        from backend.app.models.smart_sensor_history import SmartSensorHistory
        from backend.app.services.measurement_buffer import measurement_buffer
        from backend.app.services.measurement_history import flush_buffered

        sensor = await _sensor(db_session)
        measurement_buffer.record_sensor("aa:bb", "temperature", 23.4)

        await flush_buffered(db_session)

        rows = (await db_session.execute(select(SmartSensorHistory))).scalars().all()
        assert [(r.sensor_id, r.sensor_kind, r.value) for r in rows] == [(sensor.id, "temperature", 23.4)]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_reading_from_an_unadopted_sensor_is_dropped(self, db_session):
        """The farm does not track it, so there is nothing to keep — and the
        row could not be written anyway without a sensor to point at."""
        from backend.app.models.smart_sensor_history import SmartSensorHistory
        from backend.app.services.measurement_buffer import measurement_buffer
        from backend.app.services.measurement_history import flush_buffered

        measurement_buffer.record_sensor("ff:ff", "temperature", 23.4)

        await flush_buffered(db_session)

        assert (await db_session.execute(select(func.count()).select_from(SmartSensorHistory))).scalar_one() == 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_reading_for_a_deleted_plug_is_dropped_not_raised(self, db_session):
        """A plug can be removed between the report and the flush. That is a
        dropped row, not a broken loop."""
        from backend.app.services.measurement_buffer import measurement_buffer
        from backend.app.services.measurement_history import flush_buffered

        measurement_buffer.record_power(9999, 42.5)

        assert await flush_buffered(db_session) == 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_flushing_twice_does_not_write_twice(self, db_session):
        from backend.app.models.smart_plug_power_history import SmartPlugPowerHistory
        from backend.app.services.measurement_buffer import measurement_buffer
        from backend.app.services.measurement_history import flush_buffered

        plug = await _plug(db_session)
        measurement_buffer.record_power(plug.id, 42.5)
        await flush_buffered(db_session)
        await flush_buffered(db_session)

        total = (await db_session.execute(select(func.count()).select_from(SmartPlugPowerHistory))).scalar_one()
        assert total == 1


class TestSampling:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_ha_switch_stops_requests_with_credentials_and_plug_still_saved(
        self, async_client, db_session, monkeypatch, smart_plug_factory
    ):
        import httpx

        from backend.app.services import smart_plug_manager
        from backend.app.services.homeassistant import HomeAssistantService
        from backend.app.services.measurement_history import sample_polled_plugs

        monkeypatch.delenv("HA_URL", raising=False)
        monkeypatch.delenv("HA_TOKEN", raising=False)
        monkeypatch.setattr(smart_plug_manager, "homeassistant_service", HomeAssistantService())
        requests = []

        def fake_ha(request):
            requests.append(request.url.path)
            return httpx.Response(200, json={"state": "42", "message": "API running"})

        transport = httpx.MockTransport(fake_ha)

        async def handle_request(_transport, request):
            return await transport.handle_async_request(request)

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle_request)
        plug = await smart_plug_factory(plug_type="homeassistant", ha_power_entity="sensor.plug_power")
        plug_url = f"/api/v1/smart-plugs/{plug.id}"
        settings_url = "/api/v1/settings/"
        ha_config = {"ha_url": "http://127.0.0.1:8123", "ha_token": "test-ha-token"}

        enabled = await async_client.patch(settings_url, json={**ha_config, "ha_enabled": True})
        assert enabled.status_code == 200, enabled.text
        assert await sample_polled_plugs(db_session) == 1
        assert requests == ["/api/states/sensor.plug_power"]
        requests.clear()

        disabled = await async_client.patch(settings_url, json={"ha_enabled": False})
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["ha_enabled"] is False
        assert {key: disabled.json()[key] for key in ha_config} == ha_config

        sampled = await sample_polled_plugs(db_session)
        assert requests == []
        assert sampled == 0
        status = await async_client.get(f"{plug_url}/status")
        assert status.status_code == 200 and status.json()["reachable"] is False
        control = await async_client.post(f"{plug_url}/control", json={"action": "on"})
        assert control.status_code == 503
        for resource in ("entities", "sensors"):
            discovery = await async_client.get(f"/api/v1/smart-plugs/ha/{resource}")
            assert discovery.status_code == 400
        assert requests == []

        # Testing credentials is an explicit setup operation before enabling HA.
        test_connection = await async_client.post(
            "/api/v1/smart-plugs/ha/test-connection", json={"url": ha_config["ha_url"], "token": ha_config["ha_token"]}
        )
        assert test_connection.status_code == 200 and test_connection.json()["success"] is True
        assert requests == ["/api/"]
        requests.clear()

        reenabled = await async_client.patch(settings_url, json={"ha_enabled": True})
        assert reenabled.status_code == 200, reenabled.text
        assert await sample_polled_plugs(db_session) == 1
        assert requests == ["/api/states/sensor.plug_power"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_plug_that_never_reports_is_read_and_recorded(self, db_session, monkeypatch):
        """The most important test in the stage. Written inside the Zigbee
        driver, this whole feature would work for one plug type while wearing
        the application's name."""
        from backend.app.models.smart_plug_power_history import SmartPlugPowerHistory
        from backend.app.services import measurement_history
        from backend.app.services.measurement_buffer import measurement_buffer
        from backend.app.services.measurement_history import flush_buffered, sample_polled_plugs

        plug = await _plug(db_session, plug_type="tasmota")

        class _Service:
            async def get_energy(self, _plug):
                return {"power": 61.0}

        async def fake_service(_plug, _db):
            return _Service()

        monkeypatch.setattr(measurement_history.smart_plug_manager, "get_service_for_plug", fake_service)

        assert await sample_polled_plugs(db_session) == 1
        assert measurement_buffer.pending() == 1
        await flush_buffered(db_session)

        rows = (await db_session.execute(select(SmartPlugPowerHistory))).scalars().all()
        assert [(r.plug_id, r.power) for r in rows] == [(plug.id, 61.0)]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_reporting_plug_is_not_sampled(self, db_session, monkeypatch):
        """Zigbee and MQTT already push every reading. Sampling them too would
        double their rows and spend the radio for nothing."""
        from backend.app.services import measurement_history
        from backend.app.services.measurement_history import sample_polled_plugs

        await _plug(db_session, name="Z1", plug_type="zigbee", ip=None)
        await _plug(db_session, name="M1", plug_type="mqtt", ip=None)

        async def fake_service(_plug, _db):
            raise AssertionError("a reporting plug must not be polled for history")

        monkeypatch.setattr(measurement_history.smart_plug_manager, "get_service_for_plug", fake_service)

        assert await sample_polled_plugs(db_session) == 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_plug_with_no_reading_records_nothing(self, db_session, monkeypatch):
        """Never a fabricated zero: it would be drawn as "the printer was off"."""
        from backend.app.services import measurement_history
        from backend.app.services.measurement_buffer import measurement_buffer
        from backend.app.services.measurement_history import sample_polled_plugs

        await _plug(db_session, plug_type="tasmota")

        class _Service:
            async def get_energy(self, _plug):
                return None

        async def fake_service(_plug, _db):
            return _Service()

        monkeypatch.setattr(measurement_history.smart_plug_manager, "get_service_for_plug", fake_service)

        await sample_polled_plugs(db_session)

        assert measurement_buffer.pending() == 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_one_unreachable_plug_does_not_cost_the_others(self, db_session, monkeypatch):
        from backend.app.services import measurement_history
        from backend.app.services.measurement_buffer import measurement_buffer
        from backend.app.services.measurement_history import sample_polled_plugs

        await _plug(db_session, name="P1", plug_type="tasmota", ip="10.0.0.1")
        await _plug(db_session, name="P2", plug_type="tasmota", ip="10.0.0.2")

        class _Service:
            def __init__(self, ok):
                self.ok = ok

            async def get_energy(self, _plug):
                if not self.ok:
                    raise TimeoutError()
                return {"power": 12.0}

        async def fake_service(plug, _db):
            return _Service(plug.name == "P2")

        monkeypatch.setattr(measurement_history.smart_plug_manager, "get_service_for_plug", fake_service)

        await sample_polled_plugs(db_session)

        assert measurement_buffer.pending() == 1


class TestPruning:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_old_rows_go_and_recent_ones_stay(self, db_session):
        """Both halves. A sweep that takes everything is as quiet as one that
        takes nothing — the chart is simply empty either way."""
        from backend.app.models.smart_plug_power_history import SmartPlugPowerHistory
        from backend.app.services.measurement_history import prune

        plug = await _plug(db_session)
        now = datetime.now(timezone.utc)
        db_session.add(SmartPlugPowerHistory(plug_id=plug.id, power=1.0, recorded_at=now - timedelta(days=40)))
        db_session.add(SmartPlugPowerHistory(plug_id=plug.id, power=2.0, recorded_at=now - timedelta(days=1)))
        await db_session.commit()

        removed_power, _removed_sensor = await prune(db_session)

        rows = (await db_session.execute(select(SmartPlugPowerHistory))).scalars().all()
        assert removed_power == 1
        assert [r.power for r in rows] == [2.0]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_window_comes_from_the_setting(self, db_session):
        """Otherwise the control in the interface is decorative, which is worse
        than not having one."""
        from backend.app.api.routes.settings import set_setting
        from backend.app.models.smart_plug_power_history import SmartPlugPowerHistory
        from backend.app.services.measurement_history import prune

        plug = await _plug(db_session)
        now = datetime.now(timezone.utc)
        db_session.add(SmartPlugPowerHistory(plug_id=plug.id, power=1.0, recorded_at=now - timedelta(days=5)))
        await db_session.commit()

        await set_setting(db_session, "plug_power_history_retention_days", "2")
        await db_session.commit()

        await prune(db_session)

        assert (await db_session.execute(select(func.count()).select_from(SmartPlugPowerHistory))).scalar_one() == 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_each_table_has_its_own_window(self, db_session):
        from backend.app.api.routes.settings import set_setting
        from backend.app.models.smart_plug_power_history import SmartPlugPowerHistory
        from backend.app.models.smart_sensor_history import SmartSensorHistory
        from backend.app.services.measurement_history import prune

        plug = await _plug(db_session)
        sensor = await _sensor(db_session)
        old = datetime.now(timezone.utc) - timedelta(days=5)
        db_session.add(SmartPlugPowerHistory(plug_id=plug.id, power=1.0, recorded_at=old))
        db_session.add(SmartSensorHistory(sensor_id=sensor.id, sensor_kind="temperature", value=20.0, recorded_at=old))
        await db_session.commit()

        await set_setting(db_session, "plug_power_history_retention_days", "2")
        await set_setting(db_session, "sensor_history_retention_days", "90")
        await db_session.commit()

        await prune(db_session)

        assert (await db_session.execute(select(func.count()).select_from(SmartPlugPowerHistory))).scalar_one() == 0
        assert (await db_session.execute(select(func.count()).select_from(SmartSensorHistory))).scalar_one() == 1


class TestSampleInterval:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_default_is_a_minute(self, db_session):
        from backend.app.services.measurement_history import resolve_sample_seconds

        assert await resolve_sample_seconds(db_session) == 60

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_nonsense_falls_back_rather_than_stopping_the_loop(self, db_session):
        """This is read by a background loop, where an exception is a feature
        that silently stops working."""
        from backend.app.api.routes.settings import set_setting
        from backend.app.services.measurement_history import resolve_sample_seconds

        await set_setting(db_session, "plug_power_sample_seconds", "often")
        await db_session.commit()

        assert await resolve_sample_seconds(db_session) == 60
