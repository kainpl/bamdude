"""Unpairing must leave nothing running, nothing bound, and nothing to 404 on.

From the operator's log: a faulty plug was switched off and then removed. Reads
of it ran eleven seconds PAST the deletion of its row, because delete_smart_plug
unsubscribes MQTT plugs and had no Zigbee branch at all.

    14:34:12.024 [1f7d414c] read of 0x0000 failed … did not answer in time
    14:34:16.292 Deleted smart plug 'X2D Plug'   DELETE /smart-plugs/2 → 200
    14:34:27.776 [1f7d414c] read of 0x050B failed … did not answer in time
    14:34:28.652 zigpy: Removing device 0x5b8d

For sensors this is the normal case rather than the edge: a battery sensor is
asleep almost always, so "removed without being told" is what unpairing one
usually means.
"""

import asyncio
from types import SimpleNamespace

import pytest
from httpx import AsyncClient

from backend.app.models.smart_plug import SmartPlug
from backend.app.services.zigbee.sensors import sensor_store


def _plug_device(ieee):
    return SimpleNamespace(
        ieee=ieee,
        nwk=0x1234,
        manufacturer="SONOFF",
        model="S60ZBTPF",
        endpoints={0: SimpleNamespace(in_clusters={}), 1: SimpleNamespace(in_clusters={0x0006: object()})},
        node_desc=None,
    )


def _sensor_device(ieee):
    return SimpleNamespace(
        ieee=ieee,
        nwk=0x1235,
        manufacturer="SONOFF",
        model="SNZB-02D",
        endpoints={0: SimpleNamespace(in_clusters={}), 1: SimpleNamespace(in_clusters={0x0402: object()})},
        node_desc=SimpleNamespace(mac_capability_flags=SimpleNamespace(RxOnWhenIdle=False)),
    )


def _radio_with(monkeypatch, device, remove):
    """Attach a fake radio AND mark it up.

    The removal route goes through ``_require_up``, which reads the status as
    well as the application object — patching only ``_app`` yields a 409 that
    looks like a routing bug and is not one.
    """
    from backend.app.services.zigbee.coordinator import (
        CoordinatorState,
        CoordinatorStatus,
        zigbee_coordinator,
    )

    monkeypatch.setattr(zigbee_coordinator, "_app", SimpleNamespace(devices={device.ieee: device}, remove=remove))
    monkeypatch.setattr(zigbee_coordinator, "_status", CoordinatorStatus(CoordinatorState.UP))


@pytest.mark.asyncio
@pytest.mark.integration
async def test_removing_a_device_deletes_the_plug_row_that_points_at_it(
    async_client: AsyncClient, db_session, monkeypatch
):
    """Leaving the row behind gives a card bound to a device that is no longer
    on the network: unreachable for ever, with nothing on screen saying why."""
    from backend.app.services.zigbee.coordinator import zigbee_coordinator

    device = _plug_device("aa:bb:cc:dd:ee:ff:00:33")

    async def acknowledged(ieee):
        return None

    _radio_with(monkeypatch, device, acknowledged)

    plug = SmartPlug(name="Doomed", plug_type="zigbee", zigbee_ieee=device.ieee)
    db_session.add(plug)
    await db_session.commit()
    await db_session.refresh(plug)

    response = await async_client.delete(f"/api/v1/zigbee/devices/{device.ieee}")

    assert response.status_code == 200, response.text
    assert response.json()["deleted_plug_id"] == plug.id
    assert response.json()["outcome"] == "left"
    assert (await async_client.get(f"/api/v1/smart-plugs/{plug.id}")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.integration
async def test_removing_a_sensor_forgets_its_readings(async_client: AsyncClient, monkeypatch):
    from backend.app.services.zigbee.coordinator import zigbee_coordinator

    device = _sensor_device("aa:bb:cc:dd:ee:ff:00:44")

    async def acknowledged(ieee):
        return None

    _radio_with(monkeypatch, device, acknowledged)
    sensor_store.record(device.ieee, "temperature", 2341)

    await async_client.delete(f"/api/v1/zigbee/devices/{device.ieee}")

    assert sensor_store.reading(device.ieee, "temperature") is None
    assert zigbee_coordinator.applied_reporting(device.ieee) == {}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_device_that_never_answered_is_reported_as_forced(async_client: AsyncClient, monkeypatch):
    """A powered-off device never receives the leave request, keeps the network
    key, and will try to rejoin. Reporting "removed" without saying which of the
    two happened is what leaves an operator surprised weeks later."""
    from backend.app.api.routes import zigbee as routes
    from backend.app.services.zigbee.coordinator import zigbee_coordinator

    device = _plug_device("aa:bb:cc:dd:ee:ff:00:55")

    async def never_answers(ieee):
        await asyncio.sleep(60)

    _radio_with(monkeypatch, device, never_answers)
    # Patched rather than waited out: the real budget is ten seconds, and a test
    # that spends them is a test nobody runs.
    monkeypatch.setattr(routes, "_REMOVE_BUDGET_SECONDS", 0.05)

    response = await async_client.delete(f"/api/v1/zigbee/devices/{device.ieee}")

    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "forced"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_deleting_a_zigbee_plug_tears_down_its_driver_state(async_client: AsyncClient, db_session):
    """The defect the log caught: the plug row went, and the shared read task,
    the listeners and the cache entry stayed — so the radio kept being spent on
    a plug BamDude no longer managed."""
    from backend.app.services.zigbee.driver import zigbee_smart_plug_service

    plug = SmartPlug(name="Torn down", plug_type="zigbee", zigbee_ieee="aa:bb:cc:dd:ee:ff:00:66")
    db_session.add(plug)
    await db_session.commit()
    await db_session.refresh(plug)

    zigbee_smart_plug_service.update(plug.id, state="ON")
    zigbee_smart_plug_service._listeners[(plug.id, 0x0006)] = object()

    response = await async_client.delete(f"/api/v1/smart-plugs/{plug.id}")

    assert response.status_code == 200, response.text
    assert zigbee_smart_plug_service.get_plug_data(plug.id) is None
    assert [key for key in zigbee_smart_plug_service._listeners if key[0] == plug.id] == []
