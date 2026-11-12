"""Actual publication, receipts and remainder use the same commit."""

import pytest
from sqlalchemy import delete, func, select

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.order_working_plan import OrderPlanReceipt
from backend.app.models.product import ProductPlate
from backend.tests.integration.test_farm_forecast_api import _order
from backend.tests.integration.test_orders_api import catalog  # noqa: F401

pytestmark = pytest.mark.integration


async def prepared(client, db, catalog, *, count=4):
    from hashlib import sha256
    from pathlib import Path

    from backend.tests.fixtures.filament_routing_cases import write_routing_3mf

    # The planner reads metadata; publication reads the actual 3MF. Match both.
    file = catalog["file"]
    source = write_routing_3mf(
        Path(file.file_path),
        {1: [{"id": 1, "type": "PETG", "color": "#FFFFFF", "used_g": "1"}]},
        model="P1S",
        prediction=100,
    )
    file.file_hash = sha256(source.read_bytes()).hexdigest()
    file.file_size = source.stat().st_size
    file.file_metadata = {**file.file_metadata, "sliced_for_model": "P1S"}
    await db.commit()
    oid, lid = await _order(db, catalog["product"].id, count)
    plate = await db.scalar(select(ProductPlate).where(ProductPlate.product_id == catalog["product"].id))
    path = f"/api/v1/projects/{oid}/working-plan"
    entry = {"entry_id": "shade", "line_id": lid, "plate_id": plate.id, "count": count, "target": {"kind": "auto"}}
    result = await client.put(path, json={"expected_revision": 0, "entries": [entry]})
    assert result.status_code == 200, result.text
    return path, entry, result.json()


async def test_forecast_counts_live_publication_and_remaining_draft_once(committing_client, db_session, catalog):
    from backend.app.models.printer import Printer
    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.models.settings import Settings

    printer = Printer(
        name="synthetic", serial_number="SYNTHETIC-TIME", ip_address="192.0.2.1", access_code="00000000", model="P1S"
    )
    db_session.add(printer)
    await db_session.flush()
    db_session.add_all(
        [
            PrinterQueue(printer_id=printer.id),
            Settings(key="forecast_upload_seconds", value="0"),
            Settings(key="forecast_plate_clear_minutes", value="0"),
        ]
    )
    await db_session.commit()
    path, _, state = await prepared(committing_client, db_session, catalog)
    forecast_path = path.removesuffix("/working-plan") + "/forecast"
    before = (await committing_client.get(forecast_path)).json()
    assert before["now_seconds"] == 400, before
    await registered(committing_client, path, state)
    result = (await committing_client.post(path + "/submissions/test-op/execute")).json()
    assert result["status"] == "complete", result
    after = (await committing_client.get(forecast_path)).json()
    assert after["machine_seconds"] == 200
    assert after["now_seconds"] == 400, after


async def registered(client, path, state, count=2):
    body = {
        "operation_id": "test-op",
        "expected_revision": state["revision"],
        "parts": [{"entry_id": "shade", "count": count}],
    }
    result = await client.post(path + "/submissions", json=body)
    assert result.status_code == 200, result.text
    return body


async def test_legacy_enqueue_refuses_an_existing_working_plan(committing_client, db_session, catalog):
    path, entry, _ = await prepared(committing_client, db_session, catalog)
    result = await committing_client.post(
        path.removesuffix("/working-plan") + "/plan/enqueue",
        json={"items": [{k: entry[k] for k in ("line_id", "plate_id", "count")}], "target": {"kind": "auto"}},
    )
    assert result.status_code == 409, result.text
    assert result.json()["detail"]["code"] == "working_plan_required"
    assert await db_session.scalar(select(func.count()).select_from(AutoQueueItem)) == 0


ADMIN_CANCEL_RIGHTS = ["orders:read", "orders:update", "queue:update_all", "queue:read_all", "library:read_all"]


async def test_foreign_admin_can_cancel_only_unpublished_work_without_taking_over_its_author(
    committing_client, db_session, catalog
):
    from backend.app.models.order_working_plan import OrderPlanSubmission
    from backend.app.models.project import ProjectEvent
    from backend.tests.integration.test_workshop_library_rights import _jwt, _user

    path, _, state = await prepared(committing_client, db_session, catalog)
    await registered(committing_client, path, state)
    original = (await db_session.get(OrderPlanSubmission, "test-op", populate_existing=True)).actor
    reviewer = await _user(db_session, "plan-reviewer", ADMIN_CANCEL_RIGHTS)  # deliberately no queue:create
    headers = await _jwt(reviewer.username)
    read = await committing_client.get(path + "/submissions/test-op", headers=headers)
    assert read.status_code == 200, read.text
    assert read.json()["can_execute"] is False and read.json()["can_cancel"] is True
    denied = await committing_client.post(path + "/submissions/test-op/execute", headers=headers)
    assert denied.status_code in (403, 409), denied.text
    result = await committing_client.post(
        path + "/submissions/test-op/cancel-unpublished",
        headers=headers,
        json={"reason": "Owner is unavailable; keep the remaining work in the draft."},
    )
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "cancelled"
    operation = await db_session.get(OrderPlanSubmission, "test-op", populate_existing=True)
    assert operation.actor == original
    assert operation.request["cancellation"]["actor"] == f"user:{reviewer.account_identity}"
    events = list(
        await db_session.scalars(select(ProjectEvent).where(ProjectEvent.kind == "plan_submission_cancelled"))
    )
    assert len(events) == 1 and events[0].user_id == reviewer.id
    assert events[0].payload["original_actor"] == original
    assert (await committing_client.get(path)).json()["entries"][0]["count"] == 4


@pytest.mark.parametrize("missing", ["orders:update", "queue:update_all", "queue:read_all", "library:read_all"])
async def test_admin_cancellation_requires_current_rights_and_source_visibility(
    committing_client, db_session, catalog, missing
):
    from backend.app.models.order_working_plan import OrderPlanSubmission
    from backend.tests.integration.test_workshop_library_rights import _jwt, _user

    path, _, state = await prepared(committing_client, db_session, catalog)
    await registered(committing_client, path, state)
    rights = [r for r in ADMIN_CANCEL_RIGHTS if r != missing]
    rights += ["queue:read_own"]
    reviewer = await _user(db_session, "restricted-reviewer", rights)
    result = await committing_client.post(
        path + "/submissions/test-op/cancel-unpublished",
        headers=await _jwt(reviewer.username),
        json={"reason": "reviewed"},
    )
    assert result.status_code == 403, result.text
    assert (await db_session.get(OrderPlanSubmission, "test-op", populate_existing=True)).status == "pending"


async def test_admin_key_scope_is_rechecked_after_waiting_for_the_draft(
    committing_client, db_session, catalog, monkeypatch
):
    import asyncio

    from backend.app.models.api_key import APIKey
    from backend.app.models.order_working_plan import OrderPlanSubmission
    from backend.app.services import order_working_plan as working
    from backend.tests.integration.test_workshop_library_rights import _key, _user

    path, _, state = await prepared(committing_client, db_session, catalog)
    await registered(committing_client, path, state)
    reviewer = await _user(db_session, "key-reviewer", ADMIN_CANCEL_RIGHTS)
    raw = await _key(
        db_session, reviewer, can_queue=True, can_manage_projects=True, can_read_status=True, can_manage_library=True
    )
    key = await db_session.scalar(select(APIKey).where(APIKey.user_id == reviewer.id))
    entered, release = asyncio.Event(), asyncio.Event()
    original = working.lock

    async def held(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(working, "lock", held)
    task = asyncio.create_task(
        committing_client.post(
            path + "/submissions/test-op/cancel-unpublished", headers={"X-API-Key": raw}, json={"reason": "reviewed"}
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 10)
        key.printer_ids = [999999]
        await db_session.commit()
    finally:
        release.set()
    result = await asyncio.wait_for(task, 10)
    assert result.status_code == 403, result.text
    assert (await db_session.get(OrderPlanSubmission, "test-op", populate_existing=True)).status == "pending"


async def test_legacy_capture_rechecks_for_a_plan_created_while_it_waited(
    committing_client, db_session, catalog, monkeypatch
):
    import asyncio

    from backend.app.models.order_working_plan import OrderWorkingPlan
    from backend.app.services import auto_queue_add

    path, entry, _ = await prepared(committing_client, db_session, catalog)
    await db_session.execute(delete(OrderWorkingPlan))
    await db_session.commit()
    entered, release = asyncio.Event(), asyncio.Event()
    original = auto_queue_add.capture_staged

    async def held(plan):
        staged = await original(plan)
        entered.set()
        await release.wait()
        return staged

    monkeypatch.setattr(auto_queue_add, "capture_staged", held)
    task = asyncio.create_task(
        committing_client.post(
            path.removesuffix("/working-plan") + "/plan/enqueue",
            json={"items": [{k: entry[k] for k in ("line_id", "plate_id", "count")}], "target": {"kind": "auto"}},
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 10)
        saved = await committing_client.put(path, json={"expected_revision": 0, "entries": [entry]})
        assert saved.status_code == 200, saved.text
    finally:
        release.set()
    result = await asyncio.wait_for(task, 10)
    assert result.status_code == 409, result.text
    assert await db_session.scalar(select(func.count()).select_from(AutoQueueItem)) == 0
    assert (await committing_client.get(path)).json()["entries"][0]["count"] == 4


async def test_receipt_replay_does_not_publish_or_consume_twice(committing_client, db_session, catalog):
    path, entry, state = await prepared(committing_client, db_session, catalog)
    body = await registered(committing_client, path, state)
    result = await committing_client.post(path + "/submissions/test-op/execute")
    assert result.status_code == 200, result.text
    outcome = result.json()
    assert outcome["status"] == "complete", outcome
    assert len(outcome["created"][0]["queue_item_ids"]) == 2
    assert await db_session.scalar(select(func.count()).select_from(AutoQueueItem)) == 2
    assert await db_session.scalar(select(func.count()).select_from(OrderPlanReceipt)) == 1
    remainder = (await committing_client.get(path)).json()
    assert remainder["entries"][0]["count"] == 2
    assert remainder["epoch"] == 1
    assert (await committing_client.post(path + "/submissions", json=body)).json() == outcome
    assert (await committing_client.post(path + "/submissions/test-op/execute")).json() == outcome
    assert await db_session.scalar(select(func.count()).select_from(AutoQueueItem)) == 2
    await db_session.execute(delete(AutoQueueItem))
    await db_session.commit()
    assert (await committing_client.get(path + "/submissions/test-op")).json() == outcome


async def test_cancel_fences_capture_and_keeps_remainder(committing_client, db_session, catalog):
    path, entry, state = await prepared(committing_client, db_session, catalog)
    await registered(committing_client, path, state)
    result = await committing_client.post(path + "/submissions/test-op/cancel")
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "cancelled"
    assert (await committing_client.post(path + "/submissions/test-op/execute")).json()["created"] == []
    assert await db_session.scalar(select(func.count()).select_from(AutoQueueItem)) == 0
    assert (await committing_client.get(path)).json()["entries"][0]["count"] == 4


async def test_publication_failure_keeps_plan_and_receipt_atomic(committing_client, db_session, catalog, monkeypatch):
    from backend.app.services import order_plan_submission as service

    original = service.consume

    async def fail_after_receipt(*args):
        await original(*args)
        raise RuntimeError("after receipt before publisher commit")

    path, entry, state = await prepared(committing_client, db_session, catalog)
    await registered(committing_client, path, state)
    monkeypatch.setattr(service, "consume", fail_after_receipt)
    result = (await committing_client.post(path + "/submissions/test-op/execute")).json()
    assert result["created"] == []
    assert result["error"] == "publication_failed"
    assert await db_session.scalar(select(func.count()).select_from(AutoQueueItem)) == 0
    assert await db_session.scalar(select(func.count()).select_from(OrderPlanReceipt)) == 0
    assert (await committing_client.get(path)).json()["entries"][0]["count"] == 4
    monkeypatch.setattr(service, "consume", original)
    assert (await committing_client.post(path + "/submissions/test-op/execute")).json()["status"] == "complete"


@pytest.mark.parametrize("completion", ["continue", "admin_cancel"])
async def test_partial_commit_then_continue_advances_epoch_only_once(
    committing_client, db_session, catalog, monkeypatch, completion
):
    from backend.app.models.project_line import ProjectLine
    from backend.app.services import order_plan_submission as service

    path, entry, state = await prepared(committing_client, db_session, catalog)
    oid = int(path.split("/")[4])
    second_line = ProjectLine(project_id=oid, product_id=catalog["product"].id, quantity=4)
    db_session.add(second_line)
    await db_session.commit()
    response = await committing_client.put(
        path,
        json={
            "expected_revision": state["revision"],
            "entries": [entry, {**entry, "entry_id": "second", "line_id": second_line.id}],
        },
    )
    state = response.json()
    saved = await committing_client.post(
        path + "/save", json={"expected_revision": state["revision"], "expected_saved_revision": 0, "expected_epoch": 0}
    )
    assert saved.status_code == 200, saved.text
    operation = {
        "operation_id": "partial",
        "expected_revision": state["revision"],
        "parts": [{"entry_id": name, "count": 2} for name in ("shade", "second")],
    }
    assert (await committing_client.post(path + "/submissions", json=operation)).status_code == 200
    original = service.consume

    async def fail_second(db, ticket, *args):
        if ticket.part["entry"]["entry_id"] == "second":
            raise RuntimeError("second capture failed")
        await original(db, ticket, *args)

    monkeypatch.setattr(service, "consume", fail_second)
    partial = (await committing_client.post(path + "/submissions/partial/execute")).json()
    assert partial["status"] == "pending" and len(partial["created"]) == 1, partial
    state = (await committing_client.get(path)).json()
    assert state["epoch"] == 1 and state["saved_epoch"] == 0
    assert sorted(e["count"] for e in state["entries"]) == [2, 4]
    discard = await committing_client.post(
        path + "/discard",
        json={"expected_revision": state["revision"], "expected_saved_revision": 1, "expected_epoch": 1},
    )
    assert discard.status_code == 409
    monkeypatch.setattr(service, "consume", original)
    if completion == "admin_cancel":
        from backend.app.models.order_working_plan import OrderPlanSubmission
        from backend.tests.integration.test_workshop_library_rights import _jwt, _user

        operation = await db_session.get(OrderPlanSubmission, "partial", populate_existing=True)
        author = operation.actor
        reviewer = await _user(db_session, "partial-reviewer", ADMIN_CANCEL_RIGHTS)
        response = await committing_client.post(
            path + "/submissions/partial/cancel-unpublished",
            headers=await _jwt(reviewer.username),
            json={"reason": "Keep only committed work"},
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["created"] == partial["created"]
        assert result["created_count"] == 2 and result["remaining_count"] == 2
        assert await db_session.scalar(select(func.count()).select_from(AutoQueueItem)) == 2
        assert await db_session.scalar(select(func.count()).select_from(OrderPlanReceipt)) == 1
        assert (await db_session.get(OrderPlanSubmission, "partial", populate_existing=True)).actor == author
        after = (await committing_client.get(path)).json()
        assert after["entries"] == state["entries"] and after["active_operation"] is None
        return
    complete = (await committing_client.post(path + "/submissions/partial/execute")).json()
    assert complete["status"] == "complete" and len(complete["created"]) == 2, complete
    assert await db_session.scalar(select(func.count()).select_from(AutoQueueItem)) == 4
    assert (await committing_client.get(path)).json()["epoch"] == 1


async def test_printer_queue_consumes_in_its_publication(committing_client, db_session, catalog):
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.printer import Printer
    from backend.app.models.printer_queue import PrinterQueue

    path, entry, state = await prepared(committing_client, db_session, catalog)
    printer = Printer(
        name="synthetic", serial_number="SYNTHETIC", ip_address="192.0.2.1", access_code="00000000", model="P1S"
    )
    db_session.add(printer)
    await db_session.flush()
    db_session.add(PrinterQueue(printer_id=printer.id))
    await db_session.commit()
    state = (
        await committing_client.put(
            path,
            json={
                "expected_revision": state["revision"],
                "entries": [{**entry, "target": {"kind": "printer", "printer_id": printer.id}}],
            },
        )
    ).json()
    await registered(committing_client, path, state)
    result = (await committing_client.post(path + "/submissions/test-op/execute")).json()
    assert result["status"] == "complete", result
    assert result["created"][0]["queue_kind"] == "printer"
    assert await db_session.scalar(select(func.count()).select_from(PrintQueueItem)) == 2
    assert (await committing_client.get(path)).json()["entries"][0]["count"] == 2


async def test_permission_revoked_during_capture_publishes_nothing(committing_client, db_session, catalog, monkeypatch):
    from backend.app.models.order_working_plan import OrderWorkingPlan
    from backend.app.models.user import User
    from backend.app.services import auto_queue_add

    path, _, state = await prepared(committing_client, db_session, catalog)
    await registered(committing_client, path, state)
    original = auto_queue_add.capture_staged

    async def revoked(plan):
        staged = await original(plan)
        user = await db_session.scalar(select(User).where(User.username == "test_admin"))
        user.is_active = False
        await db_session.commit()
        return staged

    monkeypatch.setattr(auto_queue_add, "capture_staged", revoked)
    result = await committing_client.post(path + "/submissions/test-op/execute")
    assert result.status_code == 401, result.text
    assert await db_session.scalar(select(func.count()).select_from(AutoQueueItem)) == 0
    assert await db_session.scalar(select(func.count()).select_from(OrderPlanReceipt)) == 0
    assert (await db_session.get(OrderWorkingPlan, int(path.split("/")[4]), populate_existing=True)).entries[0][
        "count"
    ] == 4


async def test_consumed_manual_portion_does_not_become_a_zero_exclusion(committing_client, db_session, catalog):
    path, entry, state = await prepared(committing_client, db_session, catalog)
    state = (
        await committing_client.put(
            path,
            json={"expected_revision": state["revision"], "entries": [{**entry, "manual": True, "protected_count": 2}]},
        )
    ).json()
    await registered(committing_client, path, state)
    assert (await committing_client.post(path + "/submissions/test-op/execute")).json()["status"] == "complete"
    remainder = (await committing_client.get(path)).json()
    assert remainder["entries"][0]["count"] == 2
    assert remainder["entries"][0]["manual"] is False
    proposed = (
        await committing_client.post(
            path + "/replan", json={"expected_revision": remainder["revision"], "kind": "remainder"}
        )
    ).json()
    assert sum(e["count"] for e in proposed["entries"]) == 2, proposed


async def test_order_cancel_fences_and_delete_cleans_only_its_plan(committing_client, db_session, catalog):
    from backend.app.models.order_working_plan import OrderPlanSubmission, OrderWorkingPlan

    path, _, state = await prepared(committing_client, db_session, catalog)
    await registered(committing_client, path, state)
    order_path = path.removesuffix("/working-plan")
    duplicate = await committing_client.post(order_path + "/duplicate", json={"name": "copy"})
    assert duplicate.status_code == 200, duplicate.text
    assert (await committing_client.get(f"/api/v1/projects/{duplicate.json()['id']}/working-plan")).json()[
        "revision"
    ] == 0
    result = await committing_client.patch(order_path, json={"status": "cancelled"})
    assert result.status_code == 200, result.text
    assert (await committing_client.post(path + "/submissions/test-op/execute")).json()["status"] == "cancelled"
    assert (await committing_client.get(path)).json()["entries"][0]["count"] == 4
    removed = await committing_client.delete(order_path)
    assert removed.status_code == 200, removed.text
    assert await db_session.scalar(select(func.count()).select_from(OrderWorkingPlan)) == 0
    assert await db_session.scalar(select(func.count()).select_from(OrderPlanSubmission)) == 0


async def test_submission_keeps_the_reviewed_auto_eject_choice(committing_client, db_session, catalog):
    from backend.app.models.project import Project

    path, _, state = await prepared(committing_client, db_session, catalog)
    await registered(committing_client, path, state)
    order = await db_session.get(Project, int(path.split("/")[4]))
    order.auto_eject_enabled = True
    await db_session.commit()
    result = (await committing_client.post(path + "/submissions/test-op/execute")).json()
    assert result["status"] == "complete", result
    rows = (await db_session.scalars(select(AutoQueueItem))).all()
    assert len(rows) == 2
    assert all(row.auto_eject is False for row in rows)


async def test_cancel_during_real_capture_fences_late_publication(committing_client, db_session, catalog, monkeypatch):
    import asyncio

    from backend.app.services import auto_queue_add

    path, _, state = await prepared(committing_client, db_session, catalog)
    await registered(committing_client, path, state)
    entered, release = asyncio.Event(), asyncio.Event()
    original = auto_queue_add.capture_staged

    async def capture(plan):
        staged = await original(plan)
        entered.set()
        await release.wait()
        return staged

    monkeypatch.setattr(auto_queue_add, "capture_staged", capture)
    task = asyncio.create_task(committing_client.post(path + "/submissions/test-op/execute"))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        cancelled = await committing_client.post(path + "/submissions/test-op/cancel")
        assert cancelled.status_code == 200, cancelled.text
    finally:
        release.set()
    result = await asyncio.wait_for(task, 10)
    assert result.json()["status"] == "cancelled", result.text
    assert result.json()["created"] == []
    assert await db_session.scalar(select(func.count()).select_from(AutoQueueItem)) == 0
    assert await db_session.scalar(select(func.count()).select_from(OrderPlanReceipt)) == 0


async def test_two_real_publishers_of_one_operation_share_one_receipt(
    committing_client, db_session, catalog, monkeypatch
):
    import asyncio

    from backend.app.services import auto_queue_add

    path, _, state = await prepared(committing_client, db_session, catalog)
    await registered(committing_client, path, state)
    both, release = asyncio.Event(), asyncio.Event()
    count = 0
    original = auto_queue_add.capture_staged

    async def capture(plan):
        nonlocal count
        staged = await original(plan)
        count += 1
        if count == 2:
            both.set()
        await release.wait()
        return staged

    monkeypatch.setattr(auto_queue_add, "capture_staged", capture)
    tasks = [asyncio.create_task(committing_client.post(path + "/submissions/test-op/execute")) for _ in range(2)]
    try:
        await asyncio.wait_for(both.wait(), 10)
    finally:
        release.set()
    results = await asyncio.wait_for(asyncio.gather(*tasks), 15)
    assert all(r.status_code == 200 and r.json()["status"] == "complete" for r in results), [r.text for r in results]
    assert results[0].json()["created"] == results[1].json()["created"]
    assert await db_session.scalar(select(func.count()).select_from(AutoQueueItem)) == 2
    assert await db_session.scalar(select(func.count()).select_from(OrderPlanReceipt)) == 1
    assert (await committing_client.get(path)).json()["entries"][0]["count"] == 2


async def test_wizard_cancel_retains_committed_work_and_its_order_link(committing_client, db_session, catalog):
    path, _, state = await prepared(committing_client, db_session, catalog)
    await registered(committing_client, path, state)
    assert (await committing_client.post(path + "/submissions/test-op/execute")).json()["status"] == "complete"
    order_path = path.removesuffix("/working-plan")
    refused = await committing_client.delete(order_path + "?only_if_unsubmitted=true")
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "order_has_work", refused.text
    assert (await committing_client.get(order_path)).status_code == 200
    assert all(
        row.project_id == int(order_path.split("/")[-1])
        for row in (await db_session.scalars(select(AutoQueueItem))).all()
    )
    await db_session.execute(delete(AutoQueueItem))
    await db_session.commit()
    assert (await committing_client.delete(order_path + "?only_if_unsubmitted=true")).status_code == 409


async def test_wizard_cancel_removes_an_unpublished_operation(committing_client, db_session, catalog):
    path, _, state = await prepared(committing_client, db_session, catalog)
    await registered(committing_client, path, state)
    result = await committing_client.delete(path.removesuffix("/working-plan") + "?only_if_unsubmitted=true")
    assert result.status_code == 200, result.text
    assert await db_session.scalar(select(func.count()).select_from(AutoQueueItem)) == 0
    assert (await committing_client.get(path + "/submissions/test-op")).status_code == 404
