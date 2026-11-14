"""A server default affects new plan intent, never a saved operation or an explicit job."""

import json
from copy import deepcopy

import pytest
from sqlalchemy import delete, select

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.order_working_plan import OrderPlanSubmission
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.settings import Settings
from backend.tests.fixtures.filament_routing_cases import write_routing_3mf
from backend.tests.integration.test_order_plan_submission import prepared, registered
from backend.tests.integration.test_orders_api import catalog  # noqa: F401
from backend.tests.integration.test_project_filament_routing import FILAMENT, make_project, target_for

pytestmark = pytest.mark.integration


async def setting(db, enabled):
    await db.execute(delete(Settings).where(Settings.key == "default_force_color_match"))
    db.add(Settings(key="default_force_color_match", value=str(enabled).lower()))
    await db.commit()


def choice(row):
    return (
        row.force_color_match
        if isinstance(row, AutoQueueItem)
        else json.loads(row.filament_routing)["force_color_match"]
    )


async def test_default_is_off_and_the_public_ui_reads_the_saved_boolean(committing_client):
    client = committing_client
    assert (await client.get("/api/v1/settings/")).json()["default_force_color_match"] is False
    for enabled in (True, False):
        response = await client.put("/api/v1/settings/", json={"default_force_color_match": enabled})
        assert response.status_code == 200, response.text
        assert response.json()["default_force_color_match"] is enabled
        assert (await client.get("/api/v1/settings/ui-preferences")).json()["default_force_color_match"] is enabled

    # It is an ordinary Boolean, not one of the historical nullable switches.
    rejected = await client.put("/api/v1/settings/", json={"default_force_color_match": None, "currency": "EUR"})
    assert rejected.status_code == 422
    saved = (await client.get("/api/v1/settings/")).json()
    assert saved["default_force_color_match"] is False and saved["currency"] != "EUR"


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("kind", ["auto", "printer"])
async def test_legacy_plan_captures_the_default_in_each_new_job(committing_client, db_session, tmp_path, enabled, kind):
    await setting(db_session, enabled)
    source = write_routing_3mf(tmp_path / "part.gcode.3mf", {1: FILAMENT})
    _, plate, project, line = await make_project(committing_client, db_session, source, 1)
    result = await committing_client.post(
        f"/api/v1/projects/{project}/plan/enqueue",
        json={
            "items": [{"plate_id": plate, "line_id": line, "count": 2}],
            "target": await target_for(db_session, kind),
        },
    )
    assert result.status_code == 200, result.text
    model = AutoQueueItem if kind == "auto" else PrintQueueItem
    rows = list((await db_session.scalars(select(model).where(model.project_id == project))).all())
    assert len(rows) == 2 and all(choice(row) is enabled for row in rows)
    await setting(db_session, not enabled)
    for row in rows:
        await db_session.refresh(row)
        assert choice(row) is enabled  # Existing jobs are snapshots, not live defaults.


async def plan_target(client, db, catalog, kind):
    path, entry, state = await prepared(client, db, catalog)
    if kind == "printer":
        printer = Printer(
            name="colour-plan", serial_number="COLOUR-PLAN", ip_address="192.0.2.1", access_code="test", model="P1S"
        )
        db.add(printer)
        await db.flush()
        db.add(PrinterQueue(printer_id=printer.id))
        await db.commit()
        result = await client.put(
            path,
            json={
                "expected_revision": state["revision"],
                "entries": [{**entry, "target": {"kind": "printer", "printer_id": printer.id}}],
            },
        )
        assert result.status_code == 200, result.text
        state = result.json()
    return path, state


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("kind", ["auto", "printer"])
async def test_registration_freezes_colour_across_setting_change_and_both_replays(
    committing_client, db_session, catalog, enabled, kind
):
    await setting(db_session, enabled)
    path, state = await plan_target(committing_client, db_session, catalog, kind)
    request = await registered(committing_client, path, state)
    operation = await db_session.get(OrderPlanSubmission, "test-op", populate_existing=True)
    assert operation.request["parts"][0]["frozen_options"]["options"]["force_color_match"] is enabled
    await setting(db_session, not enabled)
    same = await committing_client.post(path + "/submissions", json=request)
    assert same.status_code == 200, same.text
    result = await committing_client.post(path + "/submissions/test-op/execute")
    assert result.status_code == 200 and result.json()["status"] == "complete", result.text
    model = AutoQueueItem if kind == "auto" else PrintQueueItem
    rows = list((await db_session.scalars(select(model))).all())
    assert len(rows) == 2 and all(choice(row) is enabled for row in rows)
    ids = {row.id for row in rows}
    replay = await committing_client.post(path + "/submissions/test-op/execute")
    assert replay.status_code == 200 and replay.json()["status"] == "complete", replay.text
    rows = list((await db_session.scalars(select(model))).all())
    assert {row.id for row in rows} == ids and all(choice(row) is enabled for row in rows)


@pytest.mark.parametrize("kind", ["auto", "printer"])
async def test_an_old_registered_snapshot_does_not_acquire_the_new_default(
    committing_client, db_session, catalog, kind
):
    path, state = await plan_target(committing_client, db_session, catalog, kind)
    await registered(committing_client, path, state)
    operation = await db_session.get(OrderPlanSubmission, "test-op", populate_existing=True)
    old = deepcopy(operation.request)
    for part in old["parts"]:
        part["frozen_options"]["options"].pop("force_color_match")
    operation.request = old  # A durable operation written by the pre-setting version.
    await db_session.commit()
    await setting(db_session, True)
    result = await committing_client.post(path + "/submissions/test-op/execute")
    assert result.status_code == 200 and result.json()["status"] == "complete", result.text
    model = AutoQueueItem if kind == "auto" else PrintQueueItem
    rows = list((await db_session.scalars(select(model))).all())
    assert len(rows) == 2 and all(choice(row) is False for row in rows)


@pytest.mark.parametrize("explicit", [None, False, True])
async def test_ordinary_api_jobs_keep_their_own_default_and_explicit_choice(
    committing_client, db_session, tmp_path, explicit
):
    await setting(db_session, True)
    source = write_routing_3mf(tmp_path / "api.gcode.3mf", {1: FILAMENT})
    file_id, _, _, _ = await make_project(committing_client, db_session, source, 1)
    body = {"library_file_id": file_id}
    if explicit is not None:
        body["force_color_match"] = explicit
    response = await committing_client.post("/api/v1/auto-queue/", json=body)
    assert response.status_code == 200, response.text
    row = await db_session.get(AutoQueueItem, response.json()["id"])
    assert row.force_color_match is (explicit is True)
