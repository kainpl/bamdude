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


@pytest.fixture
def paired_sensor(monkeypatch):
    from backend.app.services.zigbee.coordinator import zigbee_coordinator

    device = _sensor_device()
    monkeypatch.setattr(zigbee_coordinator, "_app", SimpleNamespace(devices={device.ieee: device}))
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
async def test_a_sensor_without_a_battery_cluster_is_not_offered_one(async_client: AsyncClient, monkeypatch):
    """The list comes from the clusters the device carries. Offering a battery
    to a mains sensor that has none would show a quantity permanently null and
    permanently stale, which reads as a fault rather than as absence."""
    from backend.app.services.zigbee.coordinator import zigbee_coordinator

    device = _sensor_device(ieee="cc:cc", battery=False)
    monkeypatch.setattr(zigbee_coordinator, "_app", SimpleNamespace(devices={device.ieee: device}))

    sensor = (await async_client.get("/api/v1/zigbee/sensors")).json()["sensors"][0]

    assert set(sensor["measurements"]) == {"temperature", "humidity"}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_mains_sensor_is_reported_as_such(async_client: AsyncClient, monkeypatch):
    from backend.app.services.zigbee.coordinator import zigbee_coordinator

    device = _sensor_device(ieee="bb:bb", rx_on_when_idle=True)
    monkeypatch.setattr(zigbee_coordinator, "_app", SimpleNamespace(devices={device.ieee: device}))

    sensor = (await async_client.get("/api/v1/zigbee/sensors")).json()["sensors"][0]

    assert sensor["power"] == "mains"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_plugs_and_the_coordinator_are_not_listed_as_sensors(async_client: AsyncClient, monkeypatch):
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
async def test_no_radio_is_an_empty_list_not_an_error(async_client: AsyncClient, monkeypatch):
    """Consistent with the device list: an install with the radio down asks this
    question and gets an answer, not a 503."""
    from backend.app.services.zigbee.coordinator import zigbee_coordinator

    monkeypatch.setattr(zigbee_coordinator, "_app", None)

    response = await async_client.get("/api/v1/zigbee/sensors")

    assert response.status_code == 200
    assert response.json() == {"sensors": []}
