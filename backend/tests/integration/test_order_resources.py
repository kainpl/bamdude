"""Generic Product A, real order accounting and queue rows; no physical commands."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.product import ProductPlate
from backend.app.models.project import Project
from backend.app.services import plan_engine
from backend.app.services.filament_policy import serialize_policy
from backend.app.services.filament_routing import RoutingPolicy
from backend.app.services.order_resources import allowed
from backend.tests.integration.test_workshop_stock_plan_regressions import archive, shop  # noqa: F401


@pytest.fixture
async def resource_shop(shop, db_session, printer_factory):
    shop["printer"].model = "A1 mini"
    shop["file"].file_metadata = {**shop["file"].file_metadata, "sliced_for_model": "A1 mini"}
    second = await printer_factory(name="Other model", model="P1S")
    from backend.app.services.printer_queues import ensure_printer_queue

    await ensure_printer_queue(db_session, second.id)
    await db_session.commit()
    return {**shop, "other": second}


async def choose(client, s, **resources):
    path = f"/api/v1/projects/{s['order']}/printer-resources"
    current = await client.get(path)
    assert current.status_code == 200, current.text
    response = await client.put(path, json={"expected_token": current.json()["token"], "resources": resources})
    assert response.status_code == 200, response.text
    return response.json()


async def pending_rows(db, s):
    printer = PrintQueueItem(
        queue_id=s["printer"].id,
        position=1,
        library_file_id=s["file"].id,
        project_id=s["order"],
        project_line_id=s["line"],
        plate_id=1,
        filament_routing=serialize_policy(RoutingPolicy(mode="auto")),
    )
    auto = AutoQueueItem(
        library_file_id=s["file"].id,
        project_id=s["order"],
        project_line_id=s["line"],
        plate_id=1,
        target_model="A1 mini",
    )
    db.add_all([printer, auto])
    await db.commit()
    return printer, auto


async def review(client, s):
    path = f"/api/v1/projects/{s['order']}/printer-resources/replan"
    current = await client.get(f"/api/v1/projects/{s['order']}/working-plan")
    response = await client.post(path, json={"expected_revision": current.json()["revision"]})
    assert response.status_code == 200, response.text
    return path, response.json()


def apply_body(proposal):
    return {
        "apply": True,
        "expected_revision": proposal["expected_revision"],
        "proposal_token": proposal["proposal_token"],
        "inputs_token": proposal["inputs_token"],
        "proposal_entries": proposal["entries"],
    }


@pytest.mark.parametrize("count", [0, 1])
@pytest.mark.parametrize("pinned", [False, True])
async def test_resource_change_preserves_zero_exclusion_without_blocking_remainder(
    committing_client, db_session, resource_shop, count, pinned
):
    """A forbidden zero row is an exclusion, never permission for a real run."""
    from copy import deepcopy

    s = resource_shop
    metadata = deepcopy(s["file"].file_metadata)
    metadata["sliced_for_model"] = "P1S"
    other_file = LibraryFile(
        filename="other-synthetic.gcode.3mf",
        file_path="other-synthetic.gcode.3mf",
        file_type="gcode",
        file_size=1,
        file_metadata=metadata,
    )
    db_session.add(other_file)
    await db_session.flush()
    other_plate = ProductPlate(product_id=s["plate"].product_id, library_file_id=other_file.id, plate_index=0)
    db_session.add(other_plate)
    await db_session.commit()
    path = f"/api/v1/projects/{s['order']}/working-plan"
    response = await committing_client.put(
        path,
        json={
            "expected_revision": 0,
            "entries": [
                {
                    "entry_id": "excluded-other-model",
                    "line_id": s["line"],
                    "plate_id": other_plate.id,
                    "count": count,
                    "manual": True,
                    "protected_count": count,
                    "target": {"kind": "printer", "printer_id": s["other"].id} if pinned else {"kind": "auto"},
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    await choose(committing_client, s, models=["A1 mini"])
    state = (await committing_client.get(path)).json()
    assert state["entries"][0]["count"] == count
    assert state["inputs_changed"] is True
    if count:
        assert {issue["code"] for issue in state["issues"]} == {"order_resources_changed"}
        return
    assert state["issues"] == []
    # Reading fresh inputs and autosaving the retained exclusion must recover.
    saved = await committing_client.put(
        path,
        json={
            "expected_revision": state["revision"],
            "inputs_token": state["inputs_token"],
            "entries": state["entries"],
        },
    )
    assert saved.status_code == 200, saved.text
    request = {
        "expected_revision": saved.json()["revision"],
        "kind": "remainder",
        "inputs_token": saved.json()["inputs_token"],
    }
    proposal = await committing_client.post(path + "/replan", json=request)
    assert proposal.status_code == 200, proposal.text
    value = proposal.json()
    assert any(e["plate_id"] == other_plate.id and e["count"] == 0 for e in value["entries"])
    assert {e["plate_id"] for e in value["entries"] if e["count"]} == {s["plate"].id}
    applied = await committing_client.post(
        path + "/replan",
        json={
            **request,
            "apply": True,
            "proposal_token": value["proposal_token"],
            "proposal_entries": value["entries"],
        },
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["issues"] == []
    assert (await committing_client.get(f"/api/v1/projects/{s['order']}/printer-resources")).json()["resources"][
        "models"
    ] == ["A1 Mini"]
    assert (await db_session.scalar(select(func.count()).select_from(AutoQueueItem))) == 0
    other_file.file_hash = "f" * 64
    await db_session.commit()
    reread = (await committing_client.get(path)).json()
    assert {issue["code"] for issue in reread["issues"]} == {"source_changed"}


@pytest.mark.parametrize("line_only", [False, True])
async def test_automatic_cross_model_rebalancing_cannot_escape_order_policy(
    committing_client, db_session, resource_shop, line_only
):
    from backend.app.services.auto_queue_eligibility import printers_for_item
    from backend.app.services.queue_rebalance import rebalance

    s = resource_shop
    await choose(committing_client, s, models=["A1 mini"], printer_ids=[s["printer"].id])
    _, auto = await pending_rows(db_session, s)
    if line_only:
        auto.project_id = None
        await db_session.commit()
    printers, _, _ = await printers_for_item(db_session, auto)
    assert [p.id for p in printers] == [s["printer"].id]
    result = await rebalance(db_session, item_ids=[auto.id], force=True)
    assert result.skipped == [(auto.id, "order_resources")]
    assert (await db_session.get(AutoQueueItem, auto.id, populate_existing=True)).target_model == "A1 mini"


async def test_model_and_printer_policy_is_order_local_and_does_not_unlink_plates(
    committing_client,
    db_session,
    resource_shop,
):
    s = resource_shop
    selected = await choose(committing_client, s, models=["A1MINI"], printer_ids=[s["printer"].id, s["other"].id])
    assert [p["id"] for p in selected["printers"] if p["allowed"]] == [s["printer"].id]
    assert await allowed(db_session, s["order"], s["printer"].id)
    assert not await allowed(db_session, s["order"], s["other"].id)
    assert not await allowed(db_session, None, s["other"].id, project_line_id=s["line"])
    assert await allowed(db_session, None, s["other"].id)
    assert await db_session.get(ProductPlate, s["plate"].id)


async def test_final_queue_claim_refuses_a_model_excluded_after_enqueue(
    committing_client, db_session, resource_shop, monkeypatch
):
    from contextlib import asynccontextmanager

    from backend.app.services.print_scheduler import PrintScheduler

    s = resource_shop
    printer, _ = await pending_rows(db_session, s)
    await choose(committing_client, s, models=["P1S"])

    @asynccontextmanager
    async def session():
        yield db_session

    monkeypatch.setattr("backend.app.services.print_scheduler.async_session", session)
    monkeypatch.setattr("backend.app.services.print_scheduler.farm_startup_guard.allowed", lambda: True)
    result = await PrintScheduler()._claim_for_dispatch(
        item_id=printer.id, queue_id=printer.queue_id, printer_id=s["printer"].id, plan_fields={}
    )
    assert not result.claimed
    assert result.reason == "order_resources_changed"
    assert (await db_session.get(PrintQueueItem, printer.id, populate_existing=True)).status == "pending"


async def test_replan_reviews_both_queues_then_replaces_only_the_shared_draft(
    committing_client,
    db_session,
    resource_shop,
):
    s = resource_shop
    await choose(committing_client, s, models=["A1 mini"], printer_ids=[s["printer"].id])
    printer, auto = await pending_rows(db_session, s)
    path, proposal = await review(committing_client, s)
    assert proposal["remove_printer_ids"] == [printer.id]
    assert proposal["remove_auto_ids"] == [auto.id]
    assert sum(e["count"] for e in proposal["entries"]) == 10
    assert await db_session.get(PrintQueueItem, printer.id)  # preview is read-only
    response = await committing_client.post(path, json=apply_body(proposal))
    assert response.status_code == 200, response.text
    assert response.json()["working_plan"]["revision"] == 1
    assert (await db_session.execute(select(func.count()).select_from(PrintQueueItem))).scalar_one() == 0
    assert (await db_session.execute(select(func.count()).select_from(AutoQueueItem))).scalar_one() == 0
    assert (await db_session.get(Project, s["order"], populate_existing=True)).status == "active"
    assert await db_session.get(ProductPlate, s["plate"].id)


async def test_rebuilding_draft_keeps_fleet_distribution_when_queues_pause(
    committing_client, db_session, resource_shop, printer_factory
):
    from backend.app.services.printer_queues import ensure_printer_queue

    client, s = committing_client, resource_shop
    printers = {"P1S": s["other"], "H2D": await printer_factory(name="H2D", model="H2D")}
    printers["P2S"] = await printer_factory(name="P2S", model="P2S")
    plates = {}
    for model, printer in printers.items():
        await ensure_printer_queue(db_session, printer.id)
        file = LibraryFile(
            filename=f"{model}.gcode.3mf",
            file_path=f"{model}.gcode.3mf",
            file_type="gcode",
            file_size=1,
            file_metadata={**s["file"].file_metadata, "sliced_for_model": model},
        )
        db_session.add(file)
        await db_session.flush()
        plate = ProductPlate(product_id=s["plate"].product_id, library_file_id=file.id, plate_index=0)
        db_session.add(plate)
        await db_session.flush()
        plates[model] = plate.id
    await db_session.commit()
    await choose(client, s, models=list(printers))
    path = f"/api/v1/projects/{s['order']}/working-plan"
    current = (await client.get(path)).json()
    saved = await client.put(path, json={"expected_revision": current["revision"], "entries": []})
    assert saved.status_code == 200, saved.text

    async def rebuild():
        current = (await client.get(path)).json()
        result = await client.post(
            path + "/replan",
            json={"expected_revision": current["revision"], "kind": "rebuild", "inputs_token": current["inputs_token"]},
        )
        assert result.status_code == 200, result.text
        return {entry["plate_id"]: entry["count"] for entry in result.json()["entries"]}

    async def pause(model, paused):
        result = await client.patch(f"/api/v1/queues/{printers[model].id}", json={"is_paused": paused})
        assert result.status_code == 200, result.text
        assert result.json()["is_paused"] == paused

    expected = await rebuild()
    assert set(expected) == set(plates.values())
    assert sum(expected.values()) == 10
    for model in printers:
        await pause(model, True)
    assert await rebuild() == {}

    selected = await choose(client, s, models=list(printers), include_paused_queues=True)
    assert selected["resources"]["include_paused_queues"] is True
    assert await rebuild() == expected
    _, resource_proposal = await review(client, s)
    assert {entry["plate_id"]: entry["count"] for entry in resource_proposal["entries"]} == expected
    forecast = await client.get(f"/api/v1/projects/{s['order']}/forecast")
    assert forecast.status_code == 200, forecast.text
    assert forecast.json()["now_seconds"] is None
    for model in ("H2D", "P2S"):
        await pause(model, False)
        assert await rebuild() == expected

    printers["P1S"].is_active = False
    await db_session.commit()
    remaining = await rebuild()
    assert set(remaining) == {plates["H2D"], plates["P2S"]}
    assert sum(remaining.values()) == 10

    whole_farm = await choose(client, s, models=None)
    assert whole_farm["resources"]["include_paused_queues"] is True
    restored = await choose(client, s, models=list(printers), include_paused_queues=False)
    assert not restored["resources"].get("include_paused_queues", False)
    await pause("P2S", True)
    assert await rebuild() == {plates["H2D"]: 10}


@pytest.mark.parametrize("change", ["new_job", "policy", "tampered"])
async def test_changed_review_refuses_before_any_queue_deletion(committing_client, db_session, resource_shop, change):
    s = resource_shop
    await choose(committing_client, s, printer_ids=[s["printer"].id])
    printer, auto = await pending_rows(db_session, s)
    path, proposal = await review(committing_client, s)
    if change == "new_job":
        await pending_rows(db_session, s)
    elif change == "policy":
        await choose(committing_client, s, models=["P1S"])
    else:
        proposal["entries"][0]["count"] += 1
    response = await committing_client.post(path, json=apply_body(proposal))
    assert response.status_code == 409, response.text
    assert await db_session.get(PrintQueueItem, printer.id, populate_existing=True)
    assert await db_session.get(AutoQueueItem, auto.id, populate_existing=True)


async def test_claimed_and_manual_work_is_preserved_and_counted_once(committing_client, db_session, resource_shop):
    s = resource_shop
    await choose(committing_client, s, models=["A1 mini"])
    printer, auto = await pending_rows(db_session, s)
    printer.manual_start = True
    run, _ = await archive(db_session, s, status="printing", quantities=(3, 1))
    claimed = PrintQueueItem(
        queue_id=s["printer"].id,
        position=0,
        project_id=s["order"],
        project_line_id=s["line"],
        library_file_id=s["file"].id,
        archive_id=run.id,
        status="printing",
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(claimed)
    await db_session.commit()
    path, proposal = await review(committing_client, s)
    assert set(proposal["preserved_printer_ids"]) == {printer.id, claimed.id}
    assert sum(e["count"] for e in proposal["entries"]) == 8
    response = await committing_client.post(path, json=apply_body(proposal))
    assert response.status_code == 200, response.text
    assert (await db_session.get(PrintQueueItem, claimed.id, populate_existing=True)).status == "printing"
    assert (await db_session.get(PrintQueueItem, printer.id, populate_existing=True)).manual_start


async def test_unattributed_preparation_refuses_a_duplicate_plan(committing_client, db_session, resource_shop):
    s = resource_shop
    printer, _ = await pending_rows(db_session, s)
    printer.status = "printing"
    printer.started_at = datetime.now(timezone.utc)
    await db_session.commit()
    response = await committing_client.post(
        f"/api/v1/projects/{s['order']}/printer-resources/replan", json={"expected_revision": 0}
    )
    assert response.status_code == 409 and response.json()["detail"]["code"] == "order_resources_preparing"


async def test_no_fallback_when_selected_machine_model_cannot_print_the_plate(
    committing_client, db_session, resource_shop
):
    s = resource_shop
    await choose(committing_client, s, printer_ids=[s["other"].id])
    await pending_rows(db_session, s)
    response = await committing_client.post(
        f"/api/v1/projects/{s['order']}/printer-resources/replan", json={"expected_revision": 0}
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "order_resources_no_plan"
