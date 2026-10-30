"""Actual add/edit routes change input context without writing the working draft."""

import pytest

from backend.tests.integration.test_farm_forecast_api import farm  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("stored_draft", [False, True])
async def test_line_add_and_edit_refresh_tokens_at_the_same_revision(committing_client, farm, stored_draft):
    client = committing_client
    product_id = farm["product"].id
    created = await client.post("/api/v1/projects/", json={"name": "Input context proof", "lines": []})
    assert created.status_code == 200, created.text
    base = f"/api/v1/projects/{created.json()['id']}"

    async def context(stage):
        working, plan = await client.get(base + "/working-plan"), await client.get(base + "/plan")
        assert working.status_code == plan.status_code == 200
        working, plan = working.json(), plan.json()
        assert working["revision"] == (plan.get("working_revision") or 0)
        assert working["inputs_token"] == plan["inputs_token"]
        print(stage, "revision=", working["revision"], "token=", working["inputs_token"])
        return working

    before = await context("new order")
    if stored_draft:
        saved = await client.put(
            base + "/working-plan",
            json={
                "expected_revision": before["revision"],
                "inputs_token": before["inputs_token"],
                "entries": [],
            },
        )
        assert saved.status_code == 200, saved.text
        before = await context("stored draft")
        assert before["revision"] > 0

    added = await client.post(
        base + "/lines/batch", json={"lines": [{"kind": "product", "product_id": product_id, "quantity": 10}]}
    )
    assert added.status_code == 200, added.text
    after_add = await context("after lines/batch")
    assert after_add["revision"] == before["revision"]
    assert after_add["inputs_token"] != before["inputs_token"]

    line = (await client.get(base)).json()["lines"][0]
    edited = await client.patch(base + f"/lines/{line['id']}", json={"quantity": 15})
    assert edited.status_code == 200, edited.text
    after_edit = await context("after line PATCH")
    assert after_edit["revision"] == after_add["revision"]
    assert after_edit["inputs_token"] != after_add["inputs_token"]
