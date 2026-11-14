"""The rebalancing switch: off unless somebody turned it on, and a PUT turns it on."""

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_failed_return_opt_in_roundtrip_and_null_is_atomic(async_client):
    before = await async_client.get("/api/v1/settings/")
    assert before.status_code == 200
    assert before.json()["auto_queue_return_failed"] is False
    for enabled in (True, False):
        saved = await async_client.put("/api/v1/settings/", json={"auto_queue_return_failed": enabled})
        assert saved.status_code == 200, saved.text
        assert saved.json()["auto_queue_return_failed"] is enabled
        assert (await async_client.get("/api/v1/settings/")).json()["auto_queue_return_failed"] is enabled
    for invalid in (None, "true", 1):
        response = await async_client.put(
            "/api/v1/settings/",
            json={
                "auto_queue_return_failed": invalid,
                "auto_queue_compatible_models": True,
            },
        )
        assert response.status_code == 422, response.text
        assert (await async_client.get("/api/v1/settings/")).json()["auto_queue_compatible_models"] is False


@pytest.mark.asyncio
async def test_rebalance_setting_defaults_off_and_round_trips(async_client):
    before = await async_client.get("/api/v1/settings/")
    assert before.status_code == 200, before.text
    assert before.json()["auto_queue_rebalance_models"] is False
    assert before.json()["auto_queue_compatible_models"] is False

    put = await async_client.put("/api/v1/settings/", json={"auto_queue_rebalance_models": True})
    assert put.status_code == 200, put.text
    assert put.json()["auto_queue_rebalance_models"] is True

    after = await async_client.get("/api/v1/settings/")
    assert after.json()["auto_queue_rebalance_models"] is True


@pytest.mark.asyncio
async def test_compatible_model_fallback_defaults_off_and_round_trips(async_client):
    before = await async_client.get("/api/v1/settings/")
    assert before.json()["auto_queue_compatible_models"] is False

    put = await async_client.put("/api/v1/settings/", json={"auto_queue_compatible_models": True})
    assert put.status_code == 200, put.text
    assert put.json()["auto_queue_compatible_models"] is True

    after = await async_client.get("/api/v1/settings/")
    assert after.json()["auto_queue_compatible_models"] is True
