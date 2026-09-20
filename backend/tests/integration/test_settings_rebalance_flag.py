"""The rebalancing switch: off unless somebody turned it on, and a PUT turns it on."""

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_rebalance_setting_defaults_off_and_round_trips(async_client):
    before = await async_client.get("/api/v1/settings/")
    assert before.status_code == 200, before.text
    assert before.json()["auto_queue_rebalance_models"] is False

    put = await async_client.put("/api/v1/settings/", json={"auto_queue_rebalance_models": True})
    assert put.status_code == 200, put.text
    assert put.json()["auto_queue_rebalance_models"] is True

    after = await async_client.get("/api/v1/settings/")
    assert after.json()["auto_queue_rebalance_models"] is True
