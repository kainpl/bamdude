"""Retained entries are readable, but never acquire another line's authority."""

import pytest

from backend.app.models.project_line import ProjectLine
from backend.tests.integration.test_farm_forecast_api import farm  # noqa: F401
from backend.tests.integration.test_order_working_plan import save_baseline, setup_plan

pytestmark = pytest.mark.integration


async def orphan(client, db, farm):
    oid, lid, _, _, state = await setup_plan(client, db, farm)
    await save_baseline(client, oid, state)
    base = f"/api/v1/projects/{oid}"
    assert (await client.delete(f"{base}/lines/{lid}")).status_code == 200
    db.add(ProjectLine(id=lid + 1, project_id=oid, product_id=farm["product"].id, quantity=10))
    await db.commit()
    state = (await client.get(base + "/working-plan")).json()
    return base, state


async def previews(client, base, state, entries=None):
    body = {
        "client_revision": "recovery",
        "inputs_token": state["inputs_token"],
        "entries": entries if entries is not None else state["entries"],
    }
    return [
        await client.post(base + "/plan/" + suffix, json=body) for suffix in ("forecast-preview", "filament-preview")
    ]


async def test_retained_orphan_preview_and_rebuild(committing_client, db_session, farm):
    base, state = await orphan(committing_client, db_session, farm)
    entries = [{**state["entries"][0], "count": 3}]
    forecast, filament = await previews(committing_client, base, state, entries)
    assert forecast.status_code == filament.status_code == 200, forecast.text
    result = forecast.json()
    assert result["issues"] == [{"entry_id": entries[0]["entry_id"], "code": "line_unavailable"}]
    assert result["workload"]["unknown_prints"] == 3
    assert result["workload"]["total_seconds"] is None
    assert not result["coverage_complete"] and result["order_ready_at"] is None
    assert result["totals"]["rows"] == 0  # No synthetic orphan row in the simulation.
    assert filament.json()["unknown_prints"] == 3
    assert not filament.json()["rows"]
    request = {"expected_revision": state["revision"], "kind": "rebuild", "inputs_token": state["inputs_token"]}
    proposal = (await committing_client.post(base + "/working-plan/replan", json=request)).json()
    assert proposal["removed_issue_entry_count"] == 1
    assert (await committing_client.get(base + "/working-plan")).json()["entries"] == state["entries"]
    applied = await committing_client.post(
        base + "/working-plan/replan",
        json={
            **request,
            "apply": True,
            "proposal_token": proposal["proposal_token"],
            "proposal_entries": proposal["entries"],
        },
    )
    assert applied.status_code == 200, applied.text
    for response in await previews(committing_client, base, applied.json()):
        assert response.status_code == 200, response.text
    # An orphan removed from the CURRENT saved draft no longer qualifies.
    for response in await previews(committing_client, base, applied.json(), entries):
        assert response.status_code == 422


@pytest.mark.parametrize("field", ["entry_id", "line_id", "plate_id", "line_token", "source_token"])
async def test_orphan_identity_cannot_be_forged(committing_client, db_session, farm, field):
    base, state = await orphan(committing_client, db_session, farm)
    entry = {**state["entries"][0]}
    entry[field] = entry[field] + 1000 if isinstance(entry[field], int) else "forged"
    for response in await previews(committing_client, base, state, [entry]):
        assert response.status_code == 422, response.text


async def test_existing_reused_line_id_is_not_retargeted(committing_client, db_session, farm):
    base, state = await orphan(committing_client, db_session, farm)
    old = state["entries"][0]
    db_session.add(
        ProjectLine(
            id=old["line_id"], project_id=int(base.rsplit("/", 1)[1]), product_id=farm["product"].id, quantity=10
        )
    )
    await db_session.commit()
    fresh = (await committing_client.get(base + "/working-plan")).json()
    forecast, filament = await previews(committing_client, base, fresh)
    assert forecast.status_code == filament.status_code == 200
    assert forecast.json()["issues"][0]["code"] == "line_unavailable"
    assert forecast.json()["totals"]["rows"] == 0


async def test_mixed_known_work_and_orphan_do_not_double_count(committing_client, db_session, farm):
    base, state = await orphan(committing_client, db_session, farm)
    old = {**state["entries"][0], "count": 3}
    known = {**old, "entry_id": "new-line", "line_id": old["line_id"] + 1, "line_token": None, "count": 2}
    forecast, filament = await previews(committing_client, base, state, [old, known])
    assert forecast.status_code == filament.status_code == 200
    result = forecast.json()
    assert result["totals"]["rows"] == 1
    assert result["workload"]["prints"] == 5
    assert result["workload"]["known_seconds"] == 7200
    assert result["workload"]["unknown_prints"] == 3
    assert result["workload"]["total_seconds"] is None
    assert filament.json()["unknown_prints"] == 3


async def test_rebuild_summary_counts_one_entry_with_multiple_issues(committing_client, db_session, farm):
    from backend.app.models.order_working_plan import OrderWorkingPlan

    base, state = await orphan(committing_client, db_session, farm)
    row = await db_session.get(OrderWorkingPlan, int(base.rsplit("/", 1)[1]), populate_existing=True)
    row.entries = [{**row.entries[0], "target": {"kind": "printer", "printer_id": 999999}}]
    await db_session.commit()
    state = (await committing_client.get(base + "/working-plan")).json()
    assert {issue["code"] for issue in state["issues"]} == {"line_unavailable", "target_unavailable"}
    response = await committing_client.post(
        base + "/working-plan/replan",
        json={"expected_revision": state["revision"], "kind": "rebuild", "inputs_token": state["inputs_token"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["removed_issue_entry_count"] == 1
