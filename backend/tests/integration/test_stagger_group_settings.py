"""The four split settings round-trip, validate, and guard the entities they name."""

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_defaults_and_round_trip(async_client):
    before = (await async_client.get("/api/v1/settings/")).json()
    assert before["stagger_split_by_tags"] is False
    assert before["stagger_group_tag_ids"] == "[]"
    assert before["stagger_split_by_location"] is False
    assert before["stagger_group_location_ids"] == "[]"

    rsp = await async_client.put(
        "/api/v1/settings/", json={"stagger_split_by_tags": True, "stagger_group_tag_ids": "[3, 1, 3]"}
    )
    assert rsp.status_code == 200, rsp.text
    after = (await async_client.get("/api/v1/settings/")).json()
    assert after["stagger_split_by_tags"] is True
    assert after["stagger_group_tag_ids"] == "[1, 3]"  # sorted, de-duplicated


@pytest.mark.parametrize("bad", ["nope", '{"a": 1}', "[true]", '["1"]'])
async def test_a_non_array_is_422(async_client, bad):
    assert (await async_client.put("/api/v1/settings/", json={"stagger_group_location_ids": bad})).status_code == 422


async def test_a_tag_that_is_a_group_is_badged_and_cannot_be_deleted(async_client):
    tag = (await async_client.post("/api/v1/printer-tags", json={"name": "Фаза 1"})).json()
    await async_client.put("/api/v1/settings/", json={"stagger_group_tag_ids": f"[{tag['id']}]"})

    listed = (await async_client.get("/api/v1/printer-tags")).json()["tags"]
    assert listed[0]["is_stagger_group"] is True
    rsp = await async_client.delete(f"/api/v1/printer-tags/{tag['id']}")
    assert rsp.status_code == 409
    assert "Staggered start" in rsp.json()["detail"]

    await async_client.put("/api/v1/settings/", json={"stagger_group_tag_ids": "[]"})
    assert (await async_client.delete(f"/api/v1/printer-tags/{tag['id']}")).status_code == 200


async def test_a_location_that_is_a_group_cannot_be_deleted(async_client):
    loc = (await async_client.post("/api/v1/printer-locations", json={"name": "Цех A"})).json()
    await async_client.put("/api/v1/settings/", json={"stagger_group_location_ids": f"[{loc['id']}]"})
    assert (await async_client.delete(f"/api/v1/printer-locations/{loc['id']}")).status_code == 409
