"""Physical recounts are independent facts, not guessed defects or stock moves."""

import pytest
from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.order_working_plan import OrderWorkingPlan
from backend.app.models.project import ProjectEvent
from backend.app.models.project_line import ProjectLine
from backend.app.schemas.order_recount import RecountWrite
from backend.app.services import order_metrics, part_stock
from backend.tests.integration.test_workshop_stock_plan_regressions import archive, plan, shop  # noqa: F401

pytestmark = pytest.mark.integration


def path(s):
    return f"/api/v1/projects/{s['order']}/lines/{s['line']}/recount"


async def read(client, s):
    response = await client.get(path(s))
    assert response.status_code == 200, response.text
    return response.json()


async def count(client, s, quantities, *, snapshot=None, reason="Physical recount"):
    snapshot = snapshot or await read(client, s)
    return await client.post(
        path(s),
        json={
            "expected_token": snapshot["token"],
            "reason": reason,
            "parts": [{"part_id": pid, "actual": n} for pid, n in quantities.items()],
        },
    )


async def test_shortage_surplus_new_print_and_batch_loader_use_the_same_signed_fact(
    committing_client, db_session, shop
):
    s = shop
    a, b = s["a"].id, s["b"].id
    original, parts = await archive(db_session, s, status="completed", quantities=(36, 10))
    before = await read(committing_client, s)
    assert {p["part_id"]: p["book"] for p in before["parts"]} == {a: 36, b: 10}
    result = await count(committing_client, s, {a: 32, b: 8}, snapshot=before)
    assert result.status_code == 200, result.text
    assert await plan(committing_client, s) == ({b: 2}, 2)
    for ctx in [
        await order_metrics.load_order_context(db_session, s["order"]),
        *(await order_metrics.batch_contexts(db_session, [s["order"]])),
    ]:
        figs = order_metrics.attribute(ctx)[0][s["line"]]
        assert [p.usable for p in figs.parts] == [32, 8]
        assert [p.count_adjustment for p in figs.parts] == [-4, -2]
        assert figs.units_printed == 8
    # A recount neither changes the archive nor invents defective print objects.
    await db_session.refresh(original)
    assert original.status == "completed"
    for row in parts:
        await db_session.refresh(row)
        assert row.defective == 0
    assert (await part_stock.balances(db_session, s["a"].product_id)) == {a: 0, b: 0}
    events = list(await db_session.scalars(select(ProjectEvent).where(ProjectEvent.kind == "parts_recounted")))
    assert len(events) == 1
    assert events[0].user_name and events[0].payload["note"] == "Physical recount"
    assert [p["delta"] for p in events[0].payload["parts"]] == [-4, -2]
    await archive(db_session, s, status="completed", quantities=(3, 1))
    after = await read(committing_client, s)
    assert [p["book"] for p in after["parts"]] == [35, 9]
    assert await plan(committing_client, s) == ({b: 1}, 1)


async def test_increase_zero_and_reversal_are_explicit_and_do_not_replay(committing_client, db_session, shop):
    s = shop
    a = s["a"].id
    await archive(db_session, s, status="completed", quantities=(6, 2))
    old = await read(committing_client, s)
    result = await count(committing_client, s, {a: 0}, snapshot=old)
    assert result.status_code == 200, result.text
    assert result.json()["parts"][0]["book"] == 0
    retry = await count(committing_client, s, {a: 0}, snapshot=old)
    assert retry.status_code == 409
    assert (await count(committing_client, s, {a: 8})).status_code == 200
    assert (await read(committing_client, s))["parts"][0]["adjustment"] == 2
    assert (await count(committing_client, s, {a: 6})).status_code == 200
    # Even an ABA (correction reversed to its original value) invalidates the old window.
    assert (await count(committing_client, s, {a: 0}, snapshot=old)).status_code == 409


async def test_finished_while_window_open_refuses_whole_request(committing_client, db_session, shop):
    s = shop
    await archive(db_session, s, status="completed", quantities=(6, 2))
    old = await read(committing_client, s)
    await archive(db_session, s, status="completed", quantities=(3, 1))
    result = await count(committing_client, s, {s["a"].id: 3, s["b"].id: 1}, snapshot=old)
    assert result.status_code == 409
    line = await db_session.get(ProjectLine, s["line"], populate_existing=True)
    assert line.part_count_adjustments is None


async def test_queued_and_running_output_is_not_counted_or_removed(committing_client, db_session, shop):
    s = shop
    await archive(db_session, s, status="completed", quantities=(6, 2))
    await archive(db_session, s, status="printing", quantities=(3, 1))
    job = AutoQueueItem(library_file_id=s["file"].id, project_id=s["order"], project_line_id=s["line"], plate_id=1)
    db_session.add(job)
    await db_session.commit()
    assert [p["book"] for p in (await read(committing_client, s))["parts"]] == [6, 2]
    assert (await count(committing_client, s, {s["b"].id: 0})).status_code == 200
    # b=0 completed + 1 running + 1 queued => 8 still needed.
    assert (await plan(committing_client, s))[0][s["b"].id] == 8
    await db_session.refresh(job)
    assert job.status == "pending"


async def test_received_and_free_stock_are_excluded_not_written_off(committing_client, db_session, shop):
    s = shop
    await archive(db_session, s, status="completed", quantities=(36, 12))
    line = await db_session.get(ProjectLine, s["line"])
    line.received = 5
    await db_session.commit()
    for part, qty in ((s["a"], 6), (s["b"], 2)):
        await part_stock.move(db_session, part_id=part.id, delta=qty, reason="surplus_banked", project_line_id=line.id)
    await db_session.commit()
    current = await read(committing_client, s)
    assert [(p["protected"], p["book"]) for p in current["parts"]] == [(21, 15), (7, 5)]
    assert (await count(committing_client, s, {s["b"].id: 0}, snapshot=current)).status_code == 200
    current = await read(committing_client, s)
    assert current["parts"][1]["usable"] == 7  # five received + two banked stay historical facts
    assert (await db_session.get(ProjectLine, s["line"], populate_existing=True)).received == 5
    assert (await part_stock.balances(db_session, s["b"].product_id))[s["b"].id] == 2


async def test_foreign_part_rolls_back_and_active_submission_refuses(committing_client, db_session, shop):
    s = shop
    await archive(db_session, s, status="completed", quantities=(6, 2))
    response = await count(committing_client, s, {s["a"].id: 3, 999999: 0})
    assert response.status_code == 409
    assert [p["book"] for p in (await read(committing_client, s))["parts"]] == [6, 2]
    db_session.add(OrderWorkingPlan(id=s["order"], active_operation="capturing"))
    await db_session.commit()
    response = await count(committing_client, s, {s["a"].id: 0})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "submission_pending"


async def test_permissions_are_checked_for_both_user_and_scoped_key(committing_client, db_session, shop):
    from backend.tests.integration.test_workshop_library_rights import _jwt, _key, _user

    s = shop
    snapshot = await read(committing_client, s)
    rights = ["orders:read", "orders:update", "products:read", "stock:read"]
    owner = await _user(db_session, "counter", rights)
    body = {"expected_token": snapshot["token"], "reason": "Counted", "parts": [{"part_id": s["a"].id, "actual": 1}]}
    assert (await committing_client.get(path(s), headers=await _jwt(owner.username))).status_code == 200
    assert (await committing_client.post(path(s), json=body, headers=await _jwt(owner.username))).status_code == 403
    owner = await _user(db_session, "scoped-counter", rights + ["stock:adjust"])
    raw = await _key(db_session, owner, can_manage_projects=True, printer_ids=[s["printer"].id])
    assert (await committing_client.get(path(s), headers={"X-API-Key": raw})).status_code == 403
    assert (await committing_client.post(path(s), json=body, headers={"X-API-Key": raw})).status_code == 403


async def test_recount_keeps_the_saved_draft(committing_client, db_session, shop):
    s = shop
    await archive(db_session, s, status="completed", quantities=(6, 2))
    old_entries = [{"entry_id": "retained", "line_id": s["line"], "plate_id": s["plate"].id, "count": 3}]
    db_session.add(OrderWorkingPlan(id=s["order"], entries=old_entries, revision=3))
    await db_session.commit()
    assert (await count(committing_client, s, {s["b"].id: 1})).status_code == 200
    row = await db_session.get(OrderWorkingPlan, s["order"], populate_existing=True)
    assert row.entries == old_entries and row.revision == 3


async def test_corrected_line_cannot_be_silently_deleted_and_closed_order_refuses(committing_client, db_session, shop):
    from backend.app.models.project import Project

    s = shop
    await archive(db_session, s, status="completed", quantities=(6, 2))
    assert (await count(committing_client, s, {s["b"].id: 1})).status_code == 200
    response = await committing_client.delete(f"/api/v1/projects/{s['order']}/lines/{s['line']}")
    assert response.status_code == 409
    project = await db_session.get(Project, s["order"])
    project.status = "completed"
    await db_session.commit()
    snapshot = await read(committing_client, s)
    assert snapshot["blocked"] == "order_recount_closed"
    assert (await count(committing_client, s, {s["b"].id: 2}, snapshot=snapshot)).status_code == 409


async def test_unfiling_cannot_resurrect_the_counted_shortage_on_free_stock(committing_client, db_session, shop):
    s = shop
    made, _ = await archive(db_session, s, status="completed", quantities=(6, 2))
    assert (await count(committing_client, s, {s["b"].id: 1})).status_code == 200
    response = await committing_client.post(
        f"/api/v1/projects/{s['order']}/remove-archives", json={"archive_ids": [made.id]}
    )
    assert response.status_code == 409, response.text
    assert (await part_stock.balances(db_session, s["a"].product_id)) == {s["a"].id: 0, s["b"].id: 0}
    response = await committing_client.delete(f"/api/v1/projects/{s['order']}")
    assert response.status_code == 409
    assert (await count(committing_client, s, {s["b"].id: 2})).status_code == 200
    response = await committing_client.post(
        f"/api/v1/projects/{s['order']}/remove-archives", json={"archive_ids": [made.id]}
    )
    assert response.status_code == 200, response.text
    assert (await part_stock.balances(db_session, s["a"].product_id)) == {s["a"].id: 6, s["b"].id: 2}


async def test_pooled_recount_refuses_later_sibling_until_reviewed_reversal(committing_client, db_session, shop):
    s = shop
    made, _ = await archive(db_session, s, status="completed", quantities=(30, 10))
    made.project_line_id = None
    await db_session.commit()
    assert (await count(committing_client, s, {s["b"].id: 5})).status_code == 200
    body = {"product_id": s["a"].product_id, "quantity": 10}
    added = await committing_client.post(f"/api/v1/projects/{s['order']}/lines", json=body)
    assert added.status_code == 409, added.text
    assert "recount" in added.text.lower()
    ctx = await order_metrics.load_order_context(db_session, s["order"])
    assert (
        sum(p.usable for f in order_metrics.attribute(ctx)[0].values() for p in f.parts if p.part_id == s["b"].id) == 5
    )
    # Explicitly undo the signed count, rather than silently crediting lost parts.
    assert (await count(committing_client, s, {s["b"].id: 10})).status_code == 200
    assert (await committing_client.post(f"/api/v1/projects/{s['order']}/lines", json=body)).status_code == 200
    edited = await committing_client.patch(f"/api/v1/projects/{s['order']}/lines/{s['line']}", json={"quantity": 1})
    assert edited.status_code == 200, edited.text
    db_session.expire_all()  # the API committed through another session
    ctx = await order_metrics.load_order_context(db_session, s["order"])
    assert (
        sum(p.usable for f in order_metrics.attribute(ctx)[0].values() for p in f.parts if p.part_id == s["b"].id) == 10
    )


@pytest.mark.parametrize("change", ["quantity", "material", "configuration"])
async def test_pooled_recount_protects_line_edits_and_configuration(committing_client, db_session, shop, change):
    s = shop
    made, _ = await archive(db_session, s, status="completed", quantities=(30, 10))
    made.project_line_id = None
    await db_session.commit()
    assert (await count(committing_client, s, {s["b"].id: 5})).status_code == 200
    url = f"/api/v1/projects/{s['order']}/lines/{s['line']}"
    if change == "configuration":
        result = await committing_client.put(
            url + "/configuration", json={"choices": {}, "part_counts": {str(s["b"].id): 2}}
        )
    else:
        result = await committing_client.patch(url, json={change: 1 if change == "quantity" else "PLA"})
    assert result.status_code == 409, result.text
    await db_session.refresh(made)
    assert made.project_line_id is None and made.status == "completed"
    assert (await read(committing_client, s))["parts"][1]["book"] == 5
    assert (await part_stock.balances(db_session, s["a"].product_id))[s["b"].id] == 0


async def test_new_pooled_output_preserves_count_and_allows_only_full_reversal_when_shared(
    committing_client, db_session, shop
):
    s = shop
    # Explicit historical output remains on its line when siblings are added.
    made, _ = await archive(db_session, s, status="completed", quantities=(30, 10))
    assert (await count(committing_client, s, {s["b"].id: 5})).status_code == 200
    added = await committing_client.post(
        f"/api/v1/projects/{s['order']}/lines", json={"product_id": s["b"].product_id, "quantity": 10}
    )
    assert added.status_code == 200, added.text
    later, _ = await archive(db_session, s, status="completed", quantities=(6, 2))
    later.project_line_id = None
    await db_session.commit()
    ctx = await order_metrics.load_order_context(db_session, s["order"])
    assert (
        sum(p.usable for f in order_metrics.attribute(ctx)[0].values() for p in f.parts if p.part_id == s["b"].id) == 7
    )
    current = await read(committing_client, s)
    assert current["blocked"] == "order_recount_shared"
    assert (await count(committing_client, s, {s["b"].id: 4}, snapshot=current)).status_code == 409
    books = {p["part_id"]: p["book"] - p["adjustment"] for p in current["parts"]}
    assert (await count(committing_client, s, books)).status_code == 200
    assert not (await db_session.get(ProjectLine, s["line"], populate_existing=True)).part_count_adjustments
    ctx = await order_metrics.load_order_context(db_session, s["order"])
    assert (
        sum(p.usable for f in order_metrics.attribute(ctx)[0].values() for p in f.parts if p.part_id == s["b"].id) == 12
    )


@pytest.mark.parametrize("same_product", [False, True])
async def test_pooled_recount_stock_guard_only_checks_requested_products(
    committing_client, db_session, shop, same_product
):
    from backend.app.models.product import Product
    from backend.app.services import finished_stock

    s = shop
    made, _ = await archive(db_session, s, status="completed", quantities=(30, 10))
    made.project_line_id = None
    await db_session.commit()
    assert (await count(committing_client, s, {s["b"].id: 5})).status_code == 200
    other = await db_session.scalar(select(Product).where(Product.name == "Product B"))
    added = await committing_client.post(
        f"/api/v1/projects/{s['order']}/lines", json={"product_id": other.id, "quantity": 2}
    )
    assert added.status_code == 200, added.text
    other_line = next(line["id"] for line in added.json()["lines"] if line["product_id"] == other.id)
    position = await finished_stock.item_for(db_session, other.id, {}, create=True)
    await finished_stock.receive(db_session, position, 1)
    await db_session.commit()
    if same_product:
        for part, qty in [(s["a"], 3), (s["b"], 1)]:
            await part_stock.move(db_session, part_id=part.id, delta=qty, reason="manual", note="test shelf")
        await db_session.commit()
    requested = {
        "line_id": s["line"] if same_product else other_line,
        "from_finished": 0 if same_product else 1,
        "kits": 1 if same_product else 0,
        "parts": {},
    }
    result = await committing_client.post(f"/api/v1/projects/{s['order']}/take-stock", json={"lines": [requested]})
    assert result.status_code == (409 if same_product else 200), result.text
    assert (await read(committing_client, s))["parts"][1]["book"] == 5
    if same_product:
        assert (await part_stock.balances(db_session, s["b"].product_id))[s["b"].id] == 1


@pytest.mark.parametrize("bad", [-1, 1.5, True, "1", 1_000_001])
def test_recount_requires_bounded_actual_integers(bad):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RecountWrite(expected_token="a" * 64, reason="counted", parts=[{"part_id": 1, "actual": bad}])
