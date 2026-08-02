"""Adoption is the existence of the row — the same gesture plugs already use.

Pairing puts a device on the network and gives it somewhere to keep its
settings. It does not mean the farm tracks it: that is a separate, deliberate
step, and un-taking it must not cost the device its pairing or its settings.
"""

from types import SimpleNamespace

import pytest
from httpx import AsyncClient

from backend.tests.zigbee_fixtures import BATTERY_SENSOR_FLAGS, fake_device

IEEE = "aa:bb:cc:dd:ee:ff:00:11"


@pytest.fixture
def paired(monkeypatch):
    from backend.app.services.zigbee.coordinator import zigbee_coordinator

    sensor = fake_device(IEEE, 0x0402, 0x0405, 0x0001, mac_capability_flags=BATTERY_SENSOR_FLAGS, asleep=True)
    monkeypatch.setattr(zigbee_coordinator, "_app", SimpleNamespace(devices={sensor.ieee: sensor}))
    return sensor


@pytest.fixture
async def known(db_session, paired):
    """Paired, so it has a row — but not adopted."""
    from backend.app.models.zigbee_device import ZigbeeDevice

    db_session.add(ZigbeeDevice(ieee=IEEE.lower(), kind="sensor", name="SONOFF SNZB-02D"))
    await db_session.commit()
    return paired


class TestAdoption:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_paired_sensor_is_not_listed_until_it_is_adopted(self, async_client: AsyncClient, known):
        """It stays on the network and keeps being configured. It is simply not
        something the farm shows or acts on yet."""
        assert (await async_client.get("/api/v1/zigbee/sensors")).json()["sensors"] == []

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_adopting_it_lists_it_under_the_name_given(self, async_client: AsyncClient, known):
        await async_client.post(
            "/api/v1/zigbee/sensors",
            json={"zigbee_ieee": IEEE, "name": "Workshop", "location": "Shop 2"},
        )

        listed = (await async_client.get("/api/v1/zigbee/sensors")).json()["sensors"]

        assert [s["name"] for s in listed] == ["Workshop"]
        assert listed[0]["location"] == "Shop 2"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_an_adopted_sensor_still_carries_its_readings(self, async_client: AsyncClient, known):
        """Adoption adds a name; it does not replace what the device reports."""
        from backend.app.services.zigbee.sensors import sensor_store

        sensor_store.forget(IEEE)
        sensor_store.record(IEEE, "temperature", 2341)
        await async_client.post("/api/v1/zigbee/sensors", json={"zigbee_ieee": IEEE, "name": "Workshop"})

        listed = (await async_client.get("/api/v1/zigbee/sensors")).json()["sensors"]

        assert listed[0]["measurements"]["temperature"]["value"] == pytest.approx(23.41)
        sensor_store.forget(IEEE)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_adopting_the_same_device_twice_is_refused(self, async_client: AsyncClient, known):
        body = {"zigbee_ieee": IEEE, "name": "Workshop"}
        await async_client.post("/api/v1/zigbee/sensors", json=body)

        assert (await async_client.post("/api/v1/zigbee/sensors", json=body)).status_code == 409

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_adopting_something_that_never_paired_is_refused(self, async_client: AsyncClient, known):
        rsp = await async_client.post("/api/v1/zigbee/sensors", json={"zigbee_ieee": "ff:ff", "name": "Ghost"})

        assert rsp.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_plug_cannot_be_adopted_as_a_sensor(self, async_client: AsyncClient, db_session, paired):
        """The device classes are closed and separate. A plug adopted here would
        appear in two places with two names and no on/off anywhere."""
        from backend.app.models.zigbee_device import ZigbeeDevice

        db_session.add(ZigbeeDevice(ieee="99:99", kind="plug", name="SONOFF S60"))
        await db_session.commit()

        rsp = await async_client.post("/api/v1/zigbee/sensors", json={"zigbee_ieee": "99:99", "name": "Nope"})

        assert rsp.status_code == 422

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_address_is_matched_however_it_was_typed(self, async_client: AsyncClient, known):
        """zigpy renders an EUI64 lower-case; an operator pastes what they see."""
        rsp = await async_client.post(
            "/api/v1/zigbee/sensors",
            json={"zigbee_ieee": IEEE.upper(), "name": "Workshop"},
        )

        assert rsp.status_code == 201
        assert (await async_client.get("/api/v1/zigbee/sensors")).json()["sensors"][0]["name"] == "Workshop"


class TestDroppingASensor:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_deleting_keeps_the_device_paired_and_its_settings(self, async_client: AsyncClient, known):
        """Un-adopting is not un-pairing. Adopting it again must restore exactly
        what it had — taking it off the network is a different action."""
        created = (
            await async_client.post("/api/v1/zigbee/sensors", json={"zigbee_ieee": IEEE, "name": "Workshop"})
        ).json()
        await async_client.put(
            f"/api/v1/zigbee/devices/{IEEE}/settings",
            json={"reporting": {"temperature": {"max_interval": 600}}},
        )

        assert (await async_client.delete(f"/api/v1/zigbee/sensors/{created['id']}")).status_code == 200

        settings = await async_client.get(f"/api/v1/zigbee/devices/{IEEE}/settings")
        assert settings.status_code == 200
        assert settings.json()["desired"]["temperature"]["max_interval"] == 600
        assert settings.json()["adopted"] is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_deleting_one_that_does_not_exist_is_a_404(self, async_client: AsyncClient, known):
        assert (await async_client.delete("/api/v1/zigbee/sensors/999")).status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_it_can_be_adopted_again_afterwards(self, async_client: AsyncClient, known):
        created = (
            await async_client.post("/api/v1/zigbee/sensors", json={"zigbee_ieee": IEEE, "name": "Workshop"})
        ).json()
        await async_client.delete(f"/api/v1/zigbee/sensors/{created['id']}")

        again = await async_client.post("/api/v1/zigbee/sensors", json={"zigbee_ieee": IEEE, "name": "Workshop 2"})

        assert again.status_code == 201


class TestRenaming:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_user_name_changes_and_the_hardware_name_does_not(self, async_client: AsyncClient, known):
        created = (
            await async_client.post("/api/v1/zigbee/sensors", json={"zigbee_ieee": IEEE, "name": "Workshop"})
        ).json()

        await async_client.patch(f"/api/v1/zigbee/sensors/{created['id']}", json={"name": "Shop 2 window"})

        listed = (await async_client.get("/api/v1/zigbee/sensors")).json()["sensors"]
        device = (await async_client.get(f"/api/v1/zigbee/devices/{IEEE}/settings")).json()
        assert listed[0]["name"] == "Shop 2 window"
        assert device["name"] == "SONOFF SNZB-02D", "the hardware name is not the operator's to change"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_location_can_be_set_and_cleared(self, async_client: AsyncClient, known):
        created = (
            await async_client.post(
                "/api/v1/zigbee/sensors", json={"zigbee_ieee": IEEE, "name": "Workshop", "location": "Shop 2"}
            )
        ).json()

        await async_client.patch(f"/api/v1/zigbee/sensors/{created['id']}", json={"location": ""})

        listed = (await async_client.get("/api/v1/zigbee/sensors")).json()["sensors"]
        assert listed[0]["location"] in (None, "")

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_renaming_one_that_does_not_exist_is_a_404(self, async_client: AsyncClient, known):
        assert (await async_client.patch("/api/v1/zigbee/sensors/999", json={"name": "x"})).status_code == 404
