"""The only way to see a reading this cycle: no rows, no page.

An absent value is null and stale, never 0. A fabricated reading is worse than a
missing one — the same rule that governs plug power, and for the same reason: a
number of the right shape gets believed.
"""

from types import SimpleNamespace

import pytest
from httpx import AsyncClient

from backend.app.services.zigbee.sensors import sensor_store
from backend.tests.zigbee_fixtures import BATTERY_SENSOR_FLAGS, MAINS_DEVICE_FLAGS, fake_device

IEEE = "aa:bb:cc:dd:ee:ff:00:11"


def _sensor_device(ieee=IEEE, rx_on_when_idle=False, battery=True):
    """Temperature, humidity and — like every real sensor — a battery cluster.

    The battery matters to the fixture: what a device is offered is derived from
    the clusters it carries, so a stub without 0x0001 is a device with no
    battery to report, not a bug in the endpoint.
    """
    clusters = (0x0402, 0x0405, *((0x0001,) if battery else ()))
    return fake_device(
        ieee,
        *clusters,
        mac_capability_flags=MAINS_DEVICE_FLAGS if rx_on_when_idle else BATTERY_SENSOR_FLAGS,
    )


async def _adopt(db, ieee, name="Workshop"):
    """Pair AND adopt. The list shows adopted sensors only — a paired device
    nobody has added stays on the network and out of the way."""
    from backend.app.models.smart_sensor import SmartSensor
    from backend.app.models.zigbee_device import ZigbeeDevice

    db.add(ZigbeeDevice(ieee=str(ieee).lower(), kind="sensor", name="SONOFF SNZB-02D"))
    db.add(SmartSensor(name=name, zigbee_ieee=str(ieee).lower()))
    await db.commit()


@pytest.fixture
async def paired_sensor(monkeypatch, db_session):
    from backend.app.services.zigbee.coordinator import zigbee_coordinator

    device = _sensor_device()
    monkeypatch.setattr(zigbee_coordinator, "_app", SimpleNamespace(devices={device.ieee: device}))
    await _adopt(db_session, device.ieee)
    sensor_store.forget(device.ieee)
    yield device
    sensor_store.forget(device.ieee)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_sensor_with_no_reading_reports_null_not_zero(async_client: AsyncClient, paired_sensor):
    response = await async_client.get("/api/v1/zigbee/sensors")

    assert response.status_code == 200, response.text
    sensor = response.json()["sensors"][0]
    assert sensor["ieee"] == IEEE
    assert sensor["power"] == "battery"
    assert sensor["measurements"]["temperature"]["value"] is None
    assert sensor["measurements"]["temperature"]["stale"] is True
    # Unknown, not "pending": nothing has been asked of this device yet, and
    # "pending" implies something is under way. Two fields because "accepted"
    # and "verified" are separate claims.
    assert sensor["measurements"]["temperature"]["reporting"] == "unknown"
    assert sensor["measurements"]["temperature"]["verification"] == "not-checked"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_reported_value_is_shown_scaled_with_its_unit(async_client: AsyncClient, paired_sensor):
    sensor_store.record(IEEE, "temperature", 2341)

    sensor = (await async_client.get("/api/v1/zigbee/sensors")).json()["sensors"][0]

    assert sensor["measurements"]["temperature"]["value"] == pytest.approx(23.41)
    assert sensor["measurements"]["temperature"]["unit"] == "°C"
    assert sensor["measurements"]["temperature"]["stale"] is False
    assert sensor["measurements"]["temperature"]["last_report_at"] is not None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_battery_is_offered_even_though_it_does_not_make_a_sensor(async_client: AsyncClient, paired_sensor):
    """A battery alone is not why anybody pairs a device, so it does not
    classify one — but once paired, its charge is worth reporting."""
    sensor_store.record(IEEE, "battery", 200)

    sensor = (await async_client.get("/api/v1/zigbee/sensors")).json()["sensors"][0]

    assert sensor["measurements"]["battery"]["value"] == pytest.approx(100.0)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_sensor_without_a_battery_cluster_is_not_offered_one(
    async_client: AsyncClient, monkeypatch, db_session
):
    """The list comes from the clusters the device carries. Offering a battery
    to a mains sensor that has none would show a quantity permanently null and
    permanently stale, which reads as a fault rather than as absence."""
    from backend.app.services.zigbee.coordinator import zigbee_coordinator

    device = _sensor_device(ieee="cc:cc", battery=False)
    monkeypatch.setattr(zigbee_coordinator, "_app", SimpleNamespace(devices={device.ieee: device}))
    await _adopt(db_session, device.ieee)

    sensor = (await async_client.get("/api/v1/zigbee/sensors")).json()["sensors"][0]

    assert set(sensor["measurements"]) == {"temperature", "humidity"}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_mains_sensor_is_reported_as_such(async_client: AsyncClient, monkeypatch, db_session):
    from backend.app.services.zigbee.coordinator import zigbee_coordinator

    device = _sensor_device(ieee="bb:bb", rx_on_when_idle=True)
    monkeypatch.setattr(zigbee_coordinator, "_app", SimpleNamespace(devices={device.ieee: device}))
    await _adopt(db_session, device.ieee)

    sensor = (await async_client.get("/api/v1/zigbee/sensors")).json()["sensors"][0]

    assert sensor["power"] == "mains"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_only_adopted_rows_are_listed_whatever_is_on_the_mesh(async_client: AsyncClient, monkeypatch):
    """Adoption is the row. A mesh full of plugs and a coordinator produces no
    sensors, because none of them has one."""
    from backend.app.services.zigbee.coordinator import zigbee_coordinator

    plug = SimpleNamespace(
        ieee="11:22",
        nwk=0x2222,
        manufacturer="SONOFF",
        model="S60ZBTPF",
        endpoints={0: SimpleNamespace(in_clusters={}), 1: SimpleNamespace(in_clusters={0x0006: object()})},
        node_desc=None,
    )
    radio = SimpleNamespace(
        ieee="00:00",
        nwk=0x0000,
        manufacturer="ITead",
        model="Dongle-M",
        endpoints={0: SimpleNamespace(in_clusters={}), 1: SimpleNamespace(in_clusters={0x0006: object()})},
        node_desc=None,
    )
    monkeypatch.setattr(zigbee_coordinator, "_app", SimpleNamespace(devices={plug.ieee: plug, radio.ieee: radio}))

    assert (await async_client.get("/api/v1/zigbee/sensors")).json()["sensors"] == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_downed_radio_does_not_erase_the_sensors(async_client: AsyncClient, monkeypatch, db_session):
    """The row, its name and its place do not live in the radio. Answering with
    an empty list reads as "BamDude forgot my sensor" rather than "cannot see
    it right now"."""
    from backend.app.services.zigbee.coordinator import zigbee_coordinator

    await _adopt(db_session, IEEE, name="Workshop")
    monkeypatch.setattr(zigbee_coordinator, "_app", None)

    body = (await async_client.get("/api/v1/zigbee/sensors")).json()

    assert [s["name"] for s in body["sensors"]] == ["Workshop"]
    assert body["sensors"][0]["present"] is False
    assert body["sensors"][0]["measurements"] == {}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_sensor_that_left_the_mesh_keeps_its_name_and_place(async_client: AsyncClient, monkeypatch, db_session):
    """A flat cell or a device carried out of range. The row is untouched, so
    it stays visible, renameable and unbindable -- none of which needs a radio."""
    from backend.app.models.printer_location import PrinterLocation
    from backend.app.models.smart_sensor import SmartSensor
    from backend.app.models.zigbee_device import ZigbeeDevice
    from backend.app.services.zigbee.coordinator import zigbee_coordinator

    place = PrinterLocation(name="Shop 2", name_key="shop 2")
    db_session.add(place)
    await db_session.commit()
    await db_session.refresh(place)
    db_session.add(ZigbeeDevice(ieee=IEEE.lower(), kind="sensor", name="SONOFF SNZB-02DR2"))
    db_session.add(SmartSensor(name="Workshop", zigbee_ieee=IEEE.lower(), location_id=place.id))
    await db_session.commit()

    # The radio is up and healthy; this one device simply is not on it.
    monkeypatch.setattr(zigbee_coordinator, "_app", SimpleNamespace(devices={}))

    sensor = (await async_client.get("/api/v1/zigbee/sensors")).json()["sensors"][0]

    assert sensor["present"] is False
    assert sensor["location"]["name"] == "Shop 2"
    assert sensor["model"] == "SONOFF SNZB-02DR2", "the hardware name recorded at pairing is all we still know"
    assert sensor["unreachable"] is True
    assert sensor["power"] is None


class TestChangingWhatWeCallItAndWhereItStands:
    """A sensor that was once given a place could never become placeless: the
    handler assigned only a non-null value, so "no location" was unsayable."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_an_explicit_null_clears_the_place(self, async_client: AsyncClient, paired_sensor, db_session):
        from sqlalchemy import select

        from backend.app.models.printer_location import PrinterLocation
        from backend.app.models.smart_sensor import SmartSensor

        place = PrinterLocation(name="Shop 2", name_key="shop 2")
        db_session.add(place)
        await db_session.commit()
        await db_session.refresh(place)
        sensor = (await db_session.execute(select(SmartSensor))).scalars().first()
        sensor.location_id = place.id
        await db_session.commit()

        body = (await async_client.patch(f"/api/v1/zigbee/sensors/{sensor.id}", json={"location_id": None})).json()

        assert body["location_id"] is None
        assert body["location"] is None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_body_without_the_key_leaves_the_place_alone(
        self, async_client: AsyncClient, paired_sensor, db_session
    ):
        """Renaming must not quietly move a sensor out of its shop."""
        from sqlalchemy import select

        from backend.app.models.printer_location import PrinterLocation
        from backend.app.models.smart_sensor import SmartSensor

        place = PrinterLocation(name="Shop 2", name_key="shop 2")
        db_session.add(place)
        await db_session.commit()
        await db_session.refresh(place)
        sensor = (await db_session.execute(select(SmartSensor))).scalars().first()
        sensor.location_id = place.id
        await db_session.commit()

        body = (await async_client.patch(f"/api/v1/zigbee/sensors/{sensor.id}", json={"name": "Bench"})).json()

        assert body["name"] == "Bench"
        assert body["location"]["name"] == "Shop 2"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_an_unknown_place_is_refused(self, async_client: AsyncClient, paired_sensor, db_session):
        from sqlalchemy import select

        from backend.app.models.smart_sensor import SmartSensor

        sensor = (await db_session.execute(select(SmartSensor))).scalars().first()

        rsp = await async_client.patch(f"/api/v1/zigbee/sensors/{sensor.id}", json={"location_id": 999})

        assert rsp.status_code == 422


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_sensor_list_answers_under_the_sensor_permission(async_client: AsyncClient):
    """It sat on smart_plugs:read, which is invisible while the two travel
    together in the default groups — and a 403 on three main pages for anybody
    who separates them."""
    from backend.app.core.auth import create_access_token, get_password_hash
    from backend.app.core.database import async_session
    from backend.app.models.group import Group
    from backend.app.models.user import User

    async with async_session() as db:
        group = Group(name="SensorsOnly", permissions=["smart_sensors:read"])
        db.add(group)
        user = User(username="sensorsonly", password_hash=get_password_hash("TestPass123!"), role="user")
        db.add(user)
        await db.commit()
        await db.refresh(group)
        await db.refresh(user)

        from sqlalchemy import text

        await db.execute(
            text("INSERT INTO user_groups (user_id, group_id) VALUES (:uid, :gid)"),
            {"uid": user.id, "gid": group.id},
        )
        await db.commit()

    token = create_access_token(data={"sub": "sensorsonly"})
    rsp = await async_client.get("/api/v1/zigbee/sensors", headers={"Authorization": f"Bearer {token}"})

    assert rsp.status_code == 200, rsp.text
