"""Product A/B only; real accounting, read-only previews and the ordinary line writer."""

import pytest
from sqlalchemy import func, select

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.product import ProductPlate
from backend.app.models.project_line import ProjectLine
from backend.app.services import farm_forecast, finished_stock, part_stock, plan_engine
from backend.tests.integration.test_workshop_stock_plan_regressions import archive, shop  # noqa: F401


async def preview(client, s, **kwargs):
    return await client.post(
        f"/api/v1/projects/{s['order']}/lines/{s['line']}/plate-fit",
        json={"around": 10, "plate_ids": [s["plate"].id], **kwargs},
    )


@pytest.mark.parametrize("variant", ["stale", "deleted", "zero"])
async def test_automatic_fit_selection_does_not_treat_working_draft_tombstones_as_candidates(
    committing_client, db_session, shop, variant
):
    from backend.app.models.order_working_plan import OrderWorkingPlan

    second_file = LibraryFile(
        filename="another.gcode.3mf",
        file_path=shop["file"].file_path,
        file_type="gcode",
        file_size=1,
        file_metadata=shop["file"].file_metadata,
    )
    db_session.add(second_file)
    await db_session.flush()
    second_plate = ProductPlate(product_id=shop["a"].product_id, library_file_id=second_file.id, plate_index=1)
    db_session.add(second_plate)
    await db_session.commit()
    path = f"/api/v1/projects/{shop['order']}/working-plan"
    response = await committing_client.put(
        path,
        json={
            "expected_revision": 0,
            "entries": [
                {
                    "entry_id": "retained",
                    "line_id": shop["line"],
                    "plate_id": shop["plate"].id,
                    "count": 0 if variant == "zero" else 1,
                    "manual": True,
                    "target": {"kind": "auto"},
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    before = response.json()["entries"]
    if variant == "stale":
        shop["file"].file_hash = "f" * 64
    elif variant == "deleted":
        await db_session.delete(shop["plate"])
    await db_session.commit()
    result = await committing_client.post(
        f"/api/v1/projects/{shop['order']}/lines/{shop['line']}/plate-fit", json={"around": 10}
    )
    assert result.status_code == 200, result.text
    assert result.json()["plate_ids"] == [second_plate.id], result.text
    stored = await db_session.get(OrderWorkingPlan, shop["order"], populate_existing=True)
    assert stored.entries == before  # read-only fit never erases an operator's plan


async def apply(client, s, response, option, ack=False):
    return await client.patch(
        f"/api/v1/projects/{s['order']}/lines/{s['line']}",
        json={
            "quantity": option["quantity"],
            "plate_fit": {
                "basis": response["basis"],
                "plate_ids": response["plate_ids"],
                "runs": option["runs"],
                "acknowledge_large_change": ack,
            },
        },
    )


@pytest.mark.parametrize("change", [None, "membership", "occupancy"])
async def test_fleet_snapshot_drives_preview_and_apply_without_printing(
    committing_client,
    db_session,
    shop,
    monkeypatch,
    change,
):
    original = dict(shop["file"].file_metadata)
    shop["file"].file_metadata = {**original, "sliced_for_model": "A1MINI"}
    second = LibraryFile(
        filename="Product A-P1S.gcode.3mf",
        file_path=shop["file"].file_path,
        file_type="gcode",
        file_size=1,
        file_metadata={
            **original,
            "sliced_for_model": "P1S",
            "plates": [
                {**original["plates"][0], "print_time_seconds": 200},
            ],
        },
    )
    db_session.add(second)
    await db_session.flush()
    plate = ProductPlate(product_id=shop["a"].product_id, library_file_id=second.id, plate_index=0)
    db_session.add(plate)
    await db_session.commit()
    fleet = plan_engine.FleetCapacity(
        [
            plan_engine.FleetMachine(1, "A1 Mini"),
            plan_engine.FleetMachine(2, "P1S"),
        ]
    )

    async def current(_db, **_kwargs):
        return fleet.copy()

    monkeypatch.setattr(plan_engine, "_current_fleet_capacity", current)
    # The normal plan compacts equivalent files; its default calculator scope
    # must retain the alternatives even without a browser-selected list.
    response = await preview(committing_client, shop, plate_ids=None)
    assert response.status_code == 200, response.text
    data = response.json()
    assert {o["quantity"] for o in data["options"]} == {9, 10, 11}
    assert all({r["plate_id"] for r in o["runs"]} == {shop["plate"].id, plate.id} for o in data["options"])
    chosen = next(o for o in data["options"] if o["quantity"] == 10)
    if change == "membership":
        fleet.machines.pop()
    elif change == "occupancy":
        fleet.machines[0].free_at = 1_000_000
    result = await apply(committing_client, shop, data, chosen)
    assert result.status_code == (409 if change else 200), result.text
    if change:
        assert result.json()["detail"]["error"] in ("plate_fit_stale", "plate_fit_invalid_plan")
    for model in (AutoQueueItem, PrintQueueItem):
        assert (
            await db_session.scalar(select(func.count()).select_from(model).where(model.project_id == shop["order"]))
            == 0
        )


async def test_extended_search_is_forwarded_per_request_without_editing_order_or_queue(
    committing_client, db_session, shop, monkeypatch
):
    from backend.app.services import plate_fit

    original = plate_fit.proposals
    modes = []

    async def observed(snapshot, request):
        modes.append(request.extended_search)
        return await original(snapshot, request)

    monkeypatch.setattr(plate_fit, "proposals", observed)
    before = (await db_session.execute(select(func.count()).select_from(AutoQueueItem))).scalar_one()
    for payload in ({"extended_search": True}, {}):
        response = await preview(committing_client, shop, **payload)
        assert response.status_code == 200 and response.json()["options"], response.text
    assert modes == [True, False]
    line = await db_session.get(ProjectLine, shop["line"], populate_existing=True)
    assert line.quantity == 10
    assert (await db_session.execute(select(func.count()).select_from(AutoQueueItem))).scalar_one() == before


@pytest.mark.parametrize("value", [1, "true", 30, None])
async def test_extended_search_rejects_non_boolean_mode(committing_client, shop, value):
    response = await preview(committing_client, shop, extended_search=value)
    assert response.status_code == 422, response.text


async def test_an_extended_preview_is_applied_by_the_ordinary_confirm(committing_client, shop):
    r = await preview(committing_client, shop, extended_search=True)
    assert r.status_code == 200, r.text
    data = r.json()
    chosen = next(o for o in data["options"] if o["quantity"] == 11)
    result = await apply(committing_client, shop, data, chosen)
    assert result.status_code == 200, result.text
    assert result.json()["lines"][0]["quantity"] == 11


@pytest.mark.parametrize("extended", [False, True])
async def test_a_preview_writes_nothing_so_it_never_holds_the_writer(committing_client, shop, test_engine, extended):
    """SQLite takes its one writer at the first write: a preview that runs no INSERT/UPDATE/DELETE never holds it."""
    from sqlalchemy import event

    writes = []

    def record(_conn, _cursor, statement, *_args):
        if statement.lstrip().split(" ", 1)[0].upper() in ("INSERT", "UPDATE", "DELETE", "REPLACE"):
            writes.append(statement.split("\n", 1)[0])

    event.listen(test_engine.sync_engine, "before_cursor_execute", record)
    try:
        response = await preview(committing_client, shop, extended_search=extended)
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", record)
    assert response.status_code == 200, response.text
    assert writes == []


async def test_preview_and_apply_create_no_jobs_or_stock_movements(committing_client, db_session, shop):
    r = await preview(committing_client, shop)
    assert r.status_code == 200, r.text
    data = r.json()
    assert [o["quantity"] for o in data["options"]] == [9, 10, 11]
    result = await apply(committing_client, shop, data, data["options"][0])
    assert result.status_code == 200, result.text
    assert result.json()["lines"][0]["quantity"] == 9
    for model in (AutoQueueItem, PrintQueueItem):
        assert await db_session.scalar(select(func.count()).select_from(model)) == 0
    assert await part_stock.reserved_parts_for_line(db_session, await db_session.get(ProjectLine, shop["line"])) == {}


async def test_free_stock_is_not_allocated_by_a_preview_but_existing_partial_stock_counts(
    committing_client, db_session, shop
):
    await part_stock.move(
        db_session, part_id=shop["a"].id, delta=300, reason="manual", note="Product A synthetic fixture"
    )
    await db_session.commit()
    r = await preview(committing_client, shop)
    assert r.status_code == 200, r.text
    assert all(part["allocated"] == 0 for o in r.json()["options"] for part in o["parts"])
    # The established intake takes partial parts; the fit reader follows that ledger.
    await part_stock.move(
        db_session,
        part_id=shop["a"].id,
        delta=-3,
        reason="reserved_parts_for_order",
        project_line_id=shop["line"],
        note=part_stock.NOTE_RESERVATION_REWRITTEN,
    )
    await db_session.commit()
    r = await preview(committing_client, shop)
    assert r.status_code == 200, r.text
    assert r.json()["options"] == []  # 3*n + 3 can never equal 3*n for this mixed plate.


async def test_completed_good_and_running_output_count_once_and_rejects_do_not(committing_client, db_session, shop):
    await archive(db_session, shop, status="completed", quantities=(6, 2), defects=(3, 1))
    await archive(db_session, shop, status="printing", quantities=(3, 1))
    r = await preview(committing_client, shop)
    assert r.status_code == 200, r.text
    current = next(o for o in r.json()["options"] if o["quantity"] == 10)
    assert sum(run["count"] for run in current["runs"]) == 8
    assert [(p["completed"], p["printing"]) for p in current["parts"]] == [(3, 3), (1, 1)]


async def test_stale_print_completion_and_large_change_are_refused_without_mutation(
    committing_client, db_session, shop
):
    r = await preview(committing_client, shop, around=30)
    assert r.status_code == 200, r.text
    data = r.json()
    chosen = data["options"][1]
    refused = await apply(committing_client, shop, data, chosen)
    assert refused.status_code == 409 and refused.json()["detail"]["error"] == "plate_fit_large_change"
    await archive(db_session, shop, status="completed", quantities=(3, 1))
    refused = await apply(committing_client, shop, data, chosen, True)
    assert refused.status_code == 409 and refused.json()["detail"]["error"] == "plate_fit_stale"
    order = await committing_client.get(f"/api/v1/projects/{shop['order']}")
    assert order.json()["lines"][0]["quantity"] == 10


@pytest.mark.parametrize(
    "payload",
    [
        {"around": 0},
        {"around": 1_000_001},
        {"around": True},
        {"around": 10, "plate_ids": []},
        {"around": 10, "plate_ids": [999999]},
    ],
)
async def test_invalid_scope_and_quantities_are_refused(committing_client, shop, payload):
    r = await preview(committing_client, shop, **payload)
    assert r.status_code in (409, 422), r.text


async def test_late_reject_recalculates_replacement_runs_and_invalidates_previous_offer(
    committing_client, db_session, shop
):
    _run, parts = await archive(db_session, shop, status="completed", quantities=(30, 10))
    first = await preview(committing_client, shop)
    assert first.status_code == 200, first.text
    complete = next(o for o in first.json()["options"] if o["quantity"] == 10)
    assert complete["runs"] == []
    parts[0].defective, parts[1].defective = 3, 1
    await db_session.commit()
    stale = await apply(committing_client, shop, first.json(), complete)
    assert stale.status_code == 409 and stale.json()["detail"]["error"] == "plate_fit_stale"
    second = await preview(committing_client, shop)
    assert second.status_code == 200, second.text
    repaired = next(o for o in second.json()["options"] if o["quantity"] == 10)
    assert repaired["runs"] == [{"plate_id": shop["plate"].id, "count": 1}]
    assert [p["new_printed"] for p in repaired["parts"]] == [3, 1]
    response = await apply(committing_client, shop, second.json(), repaired)
    assert response.status_code == 200, response.text


async def test_both_queue_tiers_are_counted_without_double_counting_handed_work(committing_client, db_session, shop):
    item = PrintQueueItem(
        queue_id=shop["printer"].id,
        library_file_id=shop["file"].id,
        plate_id=1,
        project_id=shop["order"],
        project_line_id=shop["line"],
        status="pending",
        position=1,
    )
    db_session.add(item)
    await db_session.flush()
    db_session.add_all(
        [
            AutoQueueItem(
                library_file_id=shop["file"].id,
                plate_id=1,
                project_id=shop["order"],
                project_line_id=shop["line"],
                status="pending",
                assigned_to_item_id=item.id,
            ),
            AutoQueueItem(
                library_file_id=shop["file"].id,
                plate_id=1,
                project_id=shop["order"],
                project_line_id=shop["line"],
                status="pending",
            ),
        ]
    )
    await db_session.commit()
    response = await preview(committing_client, shop)
    assert response.status_code == 200, response.text
    current = next(o for o in response.json()["options"] if o["quantity"] == 10)
    assert current["runs"] == [{"plate_id": shop["plate"].id, "count": 8}]
    assert [p["queued"] for p in current["parts"]] == [6, 2]
    item.status = "cancelled"
    await db_session.commit()
    again = await preview(committing_client, shop)
    assert next(o for o in again.json()["options"] if o["quantity"] == 10)["runs"][0]["count"] == 9


async def test_completed_line_can_browse_linked_plates_without_an_outstanding_plan(committing_client, db_session, shop):
    await archive(db_session, shop, status="completed", quantities=(30, 10))
    response = await committing_client.post(
        f"/api/v1/projects/{shop['order']}/lines/{shop['line']}/plate-fit", json={"around": 10}
    )
    assert response.status_code == 200, response.text
    assert next(o for o in response.json()["options"] if o["quantity"] == 11)["runs"][0]["count"] == 1


async def test_missing_stock_or_product_read_right_does_not_leak_a_proposal(committing_client, db_session, shop):
    from backend.tests.integration.test_workshop_library_rights import _jwt, _user

    for index, missing in enumerate(("stock:read", "products:read", "orders:update")):
        rights = [p for p in ("orders:read", "orders:update", "products:read", "stock:read") if p != missing]
        name = f"fit_restricted_{index}"
        await _user(db_session, name, rights)
        response = await committing_client.post(
            f"/api/v1/projects/{shop['order']}/lines/{shop['line']}/plate-fit",
            json={"around": 10},
            headers=await _jwt(name),
        )
        assert response.status_code == 403, response.text


async def test_reviewed_auto_removal_is_atomic_with_quantity_and_preserves_protected_rows(
    committing_client, db_session, shop
):
    from datetime import datetime

    rows = [
        AutoQueueItem(
            library_file_id=shop["file"].id,
            plate_id=1,
            project_id=shop["order"],
            project_line_id=shop["line"],
            status="pending",
            position=i,
        )
        for i in range(3)
    ]
    rows[1].manual_start = True
    rows[2].scheduled_time = datetime(2030, 1, 1)
    db_session.add_all(rows)
    await db_session.commit()
    removable_id, protected_ids = rows[0].id, [rows[1].id, rows[2].id]
    response = await preview(committing_client, shop, replace_pending=True)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["removable_auto_ids"] == [removable_id]
    # Reviewed jobs are removed from virtual lane occupancy as well as output.
    # Protected staged/scheduled jobs still occupy the forecast; preview does
    # not actually delete any row to achieve this.
    before_farm = await farm_forecast.load_snapshot(db_session, datetime.utcnow())
    without_reviewed = await farm_forecast.load_snapshot(
        db_session,
        datetime.utcnow(),
        exclude_auto_ids=(removable_id,),
    )
    assert len(before_farm.staged) == 3 and len(without_reviewed.staged) == 2
    assert await db_session.get(AutoQueueItem, removable_id) is not None
    target = next(o for o in data["options"] if o["quantity"] == 10)
    assert target["runs"][0]["count"] == 8
    payload = {
        "quantity": 10,
        "plate_fit": {
            "basis": data["basis"],
            "plate_ids": data["plate_ids"],
            "runs": target["runs"],
            "replace_pending": True,
        },
    }
    refused = await committing_client.patch(f"/api/v1/projects/{shop['order']}/lines/{shop['line']}", json=payload)
    assert refused.status_code == 409 and refused.json()["detail"]["error"] == "plate_fit_queue_change"
    assert await db_session.get(AutoQueueItem, removable_id) is not None
    payload["plate_fit"]["acknowledge_queue_change"] = True
    accepted = await committing_client.patch(f"/api/v1/projects/{shop['order']}/lines/{shop['line']}", json=payload)
    assert accepted.status_code == 200, accepted.text
    remaining = list((await db_session.execute(select(AutoQueueItem.id))).scalars())
    assert sorted(remaining) == sorted(protected_ids)
    assert await db_session.scalar(select(func.count()).select_from(PrintQueueItem)) == 0


async def test_auto_assignment_during_review_refuses_removal_and_quantity_edit(committing_client, db_session, shop):
    auto = AutoQueueItem(
        library_file_id=shop["file"].id,
        plate_id=1,
        project_id=shop["order"],
        project_line_id=shop["line"],
        status="pending",
    )
    db_session.add(auto)
    await db_session.commit()
    response = await preview(committing_client, shop, replace_pending=True)
    assert response.status_code == 200, response.text
    data = response.json()
    auto.status = "assigned"
    await db_session.commit()
    target = next(o for o in data["options"] if o["quantity"] == 9)
    payload = {
        "quantity": 9,
        "plate_fit": {
            "basis": data["basis"],
            "plate_ids": data["plate_ids"],
            "runs": target["runs"],
            "replace_pending": True,
            "acknowledge_queue_change": True,
        },
    }
    refused = await committing_client.patch(f"/api/v1/projects/{shop['order']}/lines/{shop['line']}", json=payload)
    assert refused.status_code == 409 and refused.json()["detail"]["error"] == "plate_fit_stale"
    order = await committing_client.get(f"/api/v1/projects/{shop['order']}")
    assert order.json()["lines"][0]["quantity"] == 10
    assert (await db_session.get(AutoQueueItem, auto.id)).status == "assigned"


async def test_R1_apply_timeout_keeps_quantity_stock_queue_and_journal(
    committing_client, db_session, shop, monkeypatch
):
    from backend.app.models.finished_stock import StockItemMovement
    from backend.app.models.part_stock import ProductPartStockMovement
    from backend.app.models.project import ProjectEvent
    from backend.app.services import plate_fit, stock_offers

    for part, count in ((shop["a"], 3), (shop["b"], 1)):
        await part_stock.move(db_session, part_id=part.id, delta=count, reason="manual", note="Synthetic review")
    auto = AutoQueueItem(
        library_file_id=shop["file"].id,
        plate_id=1,
        project_id=shop["order"],
        project_line_id=shop["line"],
        status="pending",
    )
    db_session.add(auto)
    await db_session.commit()
    auto_id = auto.id
    response = await preview(committing_client, shop, around=11, replace_pending=True)
    assert response.status_code == 200, response.text
    data = response.json()
    target = next(o for o in data["options"] if o["quantity"] == 11)
    assert target["from_kits"] == 1 and data["removable_auto_ids"] == [auto_id]
    models = (StockItemMovement, ProductPartStockMovement, ProjectEvent)
    before = [await db_session.scalar(select(func.count()).select_from(model)) for model in models]

    async def unexpected_write(*args, **kwargs):
        pytest.fail("timed-out validation must not reach a stock or queue writer")

    monkeypatch.setattr(stock_offers, "take", unexpected_write)
    monkeypatch.setattr(plate_fit, "remove_reviewed_pending", unexpected_write)
    monkeypatch.setattr(plate_fit, "MAX_SECONDS", 0)
    result = await committing_client.patch(
        f"/api/v1/projects/{shop['order']}/lines/{shop['line']}",
        json={
            "quantity": 11,
            "plate_fit": {
                "basis": data["basis"],
                "plate_ids": data["plate_ids"],
                "runs": target["runs"],
                "replace_pending": True,
                "acknowledge_queue_change": True,
            },
        },
    )
    assert result.status_code == 429 and result.json()["detail"]["error"] == "plate_fit_busy"
    order = await committing_client.get(f"/api/v1/projects/{shop['order']}")
    assert order.json()["lines"][0]["quantity"] == 10
    assert (await db_session.get(AutoQueueItem, auto_id)).status == "pending"
    assert [await db_session.scalar(select(func.count()).select_from(model)) for model in models] == before
    assert await part_stock.balances(db_session, shop["a"].product_id) == {
        shop["a"].id: 3,
        shop["b"].id: 1,
    }


async def separate_plates(db, shop):
    plates = []
    for name, count in (("Part A", 3), ("Part B", 1)):
        file = LibraryFile(
            filename=f"Product A {name}.gcode.3mf",
            file_path="synthetic-only",
            file_type="gcode",
            file_size=1,
            file_metadata={
                "plates": [
                    {"index": 1, "printable_objects": {str(i): name for i in range(count)}, "print_time_seconds": 100}
                ]
            },
        )
        db.add(file)
        await db.flush()
        plate = ProductPlate(product_id=shop["a"].product_id, library_file_id=file.id, plate_index=0)
        db.add(plate)
        await db.flush()
        plates.append(plate.id)
    await db.commit()
    return plates


async def test_free_finished_kits_and_loose_parts_are_greedily_used_and_reserved_only_on_apply(
    committing_client, db_session, shop
):
    ids = await separate_plates(db_session, shop)
    position = await finished_stock.item_for(db_session, shop["a"].product_id, {}, create=True)
    await finished_stock.receive(db_session, position, 2)
    for part, count in ((shop["a"], 12), (shop["b"], 3)):
        await part_stock.move(
            db_session, part_id=part.id, delta=count, reason="manual", note="Product A free-stock fixture"
        )
    await db_session.commit()
    response = await preview(committing_client, shop, plate_ids=ids)
    assert response.status_code == 200, response.text
    data = response.json()
    current = next(o for o in data["options"] if o["quantity"] == 10)
    assert (current["from_finished"], current["from_kits"], current["stock_parts"]) == (2, 3, {str(shop["a"].id): 3})
    assert [p["free_stock"] for p in current["parts"]] == [18, 5]
    assert sorted(r["count"] for r in current["runs"]) == [4, 5]
    assert await finished_stock.held_for_line(db_session, shop["line"]) == 0
    assert await part_stock.reserved_parts_for_line(db_session, await db_session.get(ProjectLine, shop["line"])) == {}
    accepted = await apply(committing_client, shop, data, current)
    assert accepted.status_code == 200, accepted.text
    assert await finished_stock.held_for_line(db_session, shop["line"]) == 2
    after = await preview(committing_client, shop, plate_ids=ids)
    assert after.status_code == 200, after.text
    same = next(o for o in after.json()["options"] if o["quantity"] == 10)
    assert [p["other_coverage"] for p in same["parts"]] == [6, 2]
    assert all(
        p["required"]
        == sum(
            p[k]
            for k in ("allocated", "completed", "printing", "queued", "other_coverage", "free_stock", "new_printed")
        )
        for p in same["parts"]
    )
    assert await part_stock.balances(db_session, shop["a"].product_id) == {shop["a"].id: 0, shop["b"].id: 0}
    assert await part_stock.reserved_parts_for_line(db_session, await db_session.get(ProjectLine, shop["line"])) == {
        shop["a"].id: 12,
        shop["b"].id: 3,
    }
    assert await db_session.scalar(select(func.count()).select_from(AutoQueueItem)) == 0
    assert await db_session.scalar(select(func.count()).select_from(PrintQueueItem)) == 0


async def test_changed_free_shelf_invalidates_the_offer_without_reserving_or_editing(
    committing_client, db_session, shop
):
    ids = await separate_plates(db_session, shop)
    response = await preview(committing_client, shop, plate_ids=ids)
    data = response.json()
    current = next(o for o in data["options"] if o["quantity"] == 9)
    await part_stock.move(db_session, part_id=shop["a"].id, delta=3, reason="manual", note="Concurrent shelf receipt")
    await db_session.commit()
    refused = await apply(committing_client, shop, data, current)
    assert refused.status_code == 409 and refused.json()["detail"]["error"] == "plate_fit_stale"
    assert (await committing_client.get(f"/api/v1/projects/{shop['order']}")).json()["lines"][0]["quantity"] == 10
    assert await part_stock.reserved_parts_for_line(db_session, await db_session.get(ProjectLine, shop["line"])) == {}


async def test_shared_free_stock_is_not_promised_to_two_lines(committing_client, db_session, shop):
    line = ProjectLine(project_id=shop["order"], product_id=shop["a"].product_id, quantity=10)
    db_session.add(line)
    for part, count in ((shop["a"], 3), (shop["b"], 1)):
        await part_stock.move(db_session, part_id=part.id, delta=count, reason="manual", note="Shared shelf")
    await db_session.commit()
    second = {**shop, "line": line.id}
    response = await preview(committing_client, second)
    assert response.status_code == 200, response.text
    current = next(o for o in response.json()["options"] if o["quantity"] == 10)
    assert current["from_finished"] == current["from_kits"] == 0
    assert current["stock_parts"] == {}
    assert current["runs"][0]["count"] == 10


async def test_stock_move_permission_is_checked_before_quantity_or_free_stock_changes(
    committing_client, db_session, shop
):
    from backend.tests.integration.test_workshop_library_rights import _jwt, _user

    for part, count in ((shop["a"], 3), (shop["b"], 1)):
        await part_stock.move(db_session, part_id=part.id, delta=count, reason="manual", note="Free stock rights")
    await db_session.commit()
    name = "fit_stock_read_only"
    await _user(db_session, name, ["orders:read", "orders:update", "products:read", "stock:read"])
    response = await committing_client.post(
        f"/api/v1/projects/{shop['order']}/lines/{shop['line']}/plate-fit",
        json={"around": 10, "plate_ids": [shop["plate"].id]},
        headers=await _jwt(name),
    )
    assert response.status_code == 200, response.text
    data = response.json()
    current = next(o for o in data["options"] if o["quantity"] == 9)
    assert current["from_kits"] == 1
    refused = await committing_client.patch(
        f"/api/v1/projects/{shop['order']}/lines/{shop['line']}",
        json={
            "quantity": 9,
            "plate_fit": {
                "basis": data["basis"],
                "plate_ids": data["plate_ids"],
                "runs": current["runs"],
                "acknowledge_large_change": False,
            },
        },
        headers=await _jwt(name),
    )
    assert refused.status_code == 403, refused.text
    assert (await committing_client.get(f"/api/v1/projects/{shop['order']}")).json()["lines"][0]["quantity"] == 10
    assert await part_stock.reserved_parts_for_line(db_session, await db_session.get(ProjectLine, shop["line"])) == {}


async def test_stock_take_failure_rolls_back_quantity_queue_removal_and_all_shelf_changes(
    committing_client, db_session, shop, monkeypatch
):
    from dataclasses import replace

    from backend.app.services import stock_offers

    for part, count in ((shop["a"], 3), (shop["b"], 1)):
        await part_stock.move(db_session, part_id=part.id, delta=count, reason="manual", note="Atomic shelf fixture")
    auto = AutoQueueItem(
        project_id=shop["order"],
        project_line_id=shop["line"],
        library_file_id=shop["file"].id,
        plate_id=1,
        status="pending",
    )
    db_session.add(auto)
    await db_session.commit()
    auto_id = auto.id
    response = await preview(committing_client, shop, replace_pending=True)
    assert response.status_code == 200, response.text
    data = response.json()
    chosen = next(o for o in data["options"] if o["quantity"] == 9)
    assert chosen["from_kits"] == 1 and chosen["runs"][0]["count"] == 8
    original = stock_offers.take

    async def clamped_after_real_take(*args, **kwargs):
        rows = await original(*args, **kwargs)
        return [replace(row, got_kits=0) for row in rows]

    monkeypatch.setattr(stock_offers, "take", clamped_after_real_take)
    refused = await committing_client.patch(
        f"/api/v1/projects/{shop['order']}/lines/{shop['line']}",
        json={
            "quantity": 9,
            "plate_fit": {
                "basis": data["basis"],
                "plate_ids": data["plate_ids"],
                "runs": chosen["runs"],
                "replace_pending": True,
                "acknowledge_queue_change": True,
            },
        },
    )
    assert refused.status_code == 409 and refused.json()["detail"]["error"] == "plate_fit_stale"
    assert (await committing_client.get(f"/api/v1/projects/{shop['order']}")).json()["lines"][0]["quantity"] == 10
    assert (await db_session.get(AutoQueueItem, auto_id)).status == "pending"
    assert await part_stock.reserved_parts_for_line(db_session, await db_session.get(ProjectLine, shop["line"])) == {}
    assert await part_stock.balances(db_session, shop["a"].product_id) == {shop["a"].id: 3, shop["b"].id: 1}


async def test_extra_demand_does_not_pretend_finished_stock_can_cover_already_completed_base_units(
    committing_client, db_session, shop
):
    # With plates of 6A+2B, q=10 needs just the 20% extras (6A+2B).
    # The free ready product cannot cover them: all ten base units already exist.
    line = await db_session.get(ProjectLine, shop["line"])
    line.extra_percentages = {str(shop["a"].id): 20, str(shop["b"].id): 20}
    file = shop["file"]
    file.file_metadata = {
        "plates": [
            {
                "index": 1,
                "printable_objects": {str(i): "Part A" if i < 6 else "Part B" for i in range(8)},
                "print_time_seconds": 100,
            }
        ]
    }
    await db_session.commit()
    await archive(db_session, shop, status="completed", quantities=(30, 10))
    position = await finished_stock.item_for(db_session, shop["a"].product_id, {}, create=True)
    await finished_stock.receive(db_session, position, 1)
    await db_session.commit()
    response = await preview(committing_client, shop, around=12)
    assert response.status_code == 200, response.text
    lower = max((o for o in response.json()["options"] if o["quantity"] < 12), key=lambda o: o["quantity"])
    assert lower["quantity"] == 10
    assert lower["from_finished"] == 0 and lower["runs"][0]["count"] == 1


async def test_free_stock_clamps_to_small_demand_but_does_not_hide_infeasibility_above_it(
    committing_client, db_session, shop
):
    ids = await separate_plates(db_session, shop)
    for part, count in ((shop["a"], 7), (shop["b"], 1)):
        await part_stock.move(
            db_session, part_id=part.id, delta=count, reason="manual", note="Stock saturation boundary"
        )
    await db_session.commit()
    response = await preview(committing_client, shop, plate_ids=ids)
    assert response.status_code == 200, response.text
    data = response.json()
    assert [o["quantity"] for o in data["options"]] == [2]
    assert data["upper"] == "none" and data["current"] == "none"
    lower = data["options"][0]
    assert lower["stock_parts"] == {str(shop["a"].id): 3} and lower["from_kits"] == 1
    assert [p["free_stock"] for p in lower["parts"]] == [6, 1]
    assert lower["runs"] == [{"plate_id": ids[1], "count": 1}]
    assert await part_stock.balances(db_session, shop["a"].product_id) == {shop["a"].id: 7, shop["b"].id: 1}
