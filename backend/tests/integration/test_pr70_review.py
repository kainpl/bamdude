"""Catalog edits cannot silently discard snapshotted extra-part obligations."""

import pytest
from sqlalchemy import func, select

from backend.app.models.customer import Customer
from backend.app.models.part_stock import ProductPartStockMovement
from backend.app.models.project import ProjectEvent
from backend.app.models.project_line import ProjectLine, ProjectLinePartStock
from backend.app.services import part_stock
from backend.tests.integration.test_workshop_extra_parts import extra_order, state
from backend.tests.integration.test_workshop_stock_plan_regressions import archive, shop as shop

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize("received_extra", [0, 3])
@pytest.mark.parametrize("edit", [{"qty_per_unit": 0, "ignored": True}, {"qty_per_unit": 0}, {"qty_per_unit": 2}])
async def test_catalog_edit_preserves_extras_and_both_completion_gates(
    committing_client, db_session, shop, received_extra, edit
):
    s = await extra_order(committing_client, db_session, shop, a=10, b=0)
    customer = Customer(name="Product A synthetic customer")
    db_session.add(customer)
    await db_session.commit()
    assert (
        await committing_client.patch(f"/api/v1/projects/{s['order']}", json={"customer_id": customer.id})
    ).status_code == 200
    await archive(db_session, s, status="completed", quantities=(33, 10))
    url = f"/api/v1/projects/{s['order']}/fulfilment"
    response = await committing_client.post(
        url,
        json={
            "lines": [
                {
                    "line_id": s["line"],
                    "receive": 10,
                    "issue": 10,
                    "parts": [{"part_id": s["a"].id, "receive": received_extra}],
                }
            ],
            "recipient": {"name": "Synthetic recipient"},
        },
    )
    assert response.status_code == 200, response.text
    before = await state(committing_client, s)
    line = await db_session.get(ProjectLine, s["line"])
    snapshot = dict(line.extra_percentages)
    models = (ProductPartStockMovement, ProjectEvent)
    counts = [await db_session.scalar(select(func.count()).select_from(m)) for m in models]
    balance = await part_stock.balances(db_session, s["a"].product_id)
    response = await committing_client.patch(f"/api/v1/products/{s['a'].product_id}/parts/{s['a'].id}", json=edit)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "This part has additional quantities in an order; keep it as a separate part"
    assert await state(committing_client, s) == before
    await db_session.refresh(line)
    await db_session.refresh(s["a"])
    assert line.extra_percentages == snapshot
    assert s["a"].qty_per_unit == 3 and not s["a"].ignored
    assert await part_stock.balances(db_session, s["a"].product_id) == balance
    assert [await db_session.scalar(select(func.count()).select_from(m)) for m in models] == counts
    assert before["can_complete"] is False
    for response in (
        await committing_client.patch(f"/api/v1/projects/{s['order']}", json={"status": "completed"}),
        await committing_client.post(url, json={"lines": [], "complete": True}),
    ):
        assert response.status_code == 409, response.text
    # The obligation remains visible, receivable and issuable after the refusal.
    response = await committing_client.post(
        url,
        json={
            "lines": [
                {"line_id": s["line"], "parts": [{"part_id": s["a"].id, "receive": 3 - received_extra, "issue": 3}]}
            ],
            "recipient": {"name": "Synthetic recipient"},
            "complete": True,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["order"]["status"] == "completed"


@pytest.mark.parametrize("received_extra", [0, 3])
async def test_refused_ignore_then_cancel_returns_held_extras_once(committing_client, db_session, shop, received_extra):
    s = await extra_order(committing_client, db_session, shop, a=10, b=0)
    await archive(db_session, s, status="completed", quantities=(33, 10))
    response = await committing_client.post(
        f"/api/v1/projects/{s['order']}/fulfilment",
        json={
            "lines": [
                {"line_id": s["line"], "receive": 10, "parts": [{"part_id": s["a"].id, "receive": received_extra}]}
            ],
        },
    )
    assert response.status_code == 200, response.text
    response = await committing_client.patch(
        f"/api/v1/products/{s['a'].product_id}/parts/{s['a'].id}", json={"qty_per_unit": 0, "ignored": True}
    )
    assert response.status_code == 409, response.text
    for _ in range(2):
        response = await committing_client.patch(f"/api/v1/projects/{s['order']}", json={"status": "cancelled"})
        assert response.status_code == 200, response.text
        assert await part_stock.balances(db_session, s["a"].product_id) == {s["a"].id: received_extra, s["b"].id: 0}
        row = await db_session.get(ProjectLinePartStock, (s["line"], s["a"].id))
        if row is not None:
            assert row.returned == received_extra and part_stock.part_held(row) == 0


async def test_display_edits_defaults_and_unchanged_count_remain_allowed(committing_client, db_session, shop):
    s = await extra_order(committing_client, db_session, shop, a=10, b=0)
    response = await committing_client.patch(
        f"/api/v1/products/{s['a'].product_id}/parts/{s['a'].id}",
        json={
            "name": "Renamed Part A",
            "qty_per_unit": 3,
            "extra_percent": 20,
        },
    )
    assert response.status_code == 200, response.text
    line = await db_session.get(ProjectLine, s["line"])
    assert line.extra_percentages == {str(s["a"].id): 10}
    # A part with no extra snapshot retains ordinary live catalogue counts.
    response = await committing_client.patch(
        f"/api/v1/products/{s['b'].product_id}/parts/{s['b'].id}", json={"qty_per_unit": 0}
    )
    assert response.status_code == 200, response.text


async def test_line_cannot_hide_received_extras_but_can_reconfigure_before_movements(
    committing_client, db_session, shop
):
    s = await extra_order(committing_client, db_session, shop, a=10, b=0)
    url = f"/api/v1/projects/{s['order']}/lines/{s['line']}/configuration"
    before = await state(committing_client, s)
    response = await committing_client.put(url, json={"part_counts": {str(s["a"].id): 0}, "choices": {}})
    assert response.status_code == 200, response.text
    revision = response.json()["lines"][0]["configuration"]["revision"]
    response = await committing_client.put(url, json={"part_counts": {}, "choices": {}, "expected_revision": revision})
    assert response.status_code == 200, response.text
    revision = response.json()["lines"][0]["configuration"]["revision"]
    assert revision == before["lines"][0]["configuration"]["revision"] + 2
    before["lines"][0]["configuration"]["revision"] = revision
    assert await state(committing_client, s) == before
    await archive(db_session, s, status="completed", quantities=(33, 10))
    response = await committing_client.post(
        f"/api/v1/projects/{s['order']}/fulfilment",
        json={
            "lines": [{"line_id": s["line"], "parts": [{"part_id": s["a"].id, "receive": 3}]}],
        },
    )
    assert response.status_code == 200, response.text
    before = await state(committing_client, s)
    response = await committing_client.put(
        url, json={"part_counts": {str(s["a"].id): 0}, "choices": {}, "expected_revision": revision}
    )
    assert response.status_code == 409, response.text
    assert await state(committing_client, s) == before


@pytest.mark.parametrize("receipt", ["main", "extra"])
async def test_extra_receipts_keep_material_eligibility(committing_client, db_session, shop, receipt):
    s = await extra_order(committing_client, db_session, shop, a=10, b=0)
    assert (
        await committing_client.patch(f"/api/v1/projects/{s['order']}/lines/{s['line']}", json={"material": "PLA"})
    ).status_code == 200
    response = await committing_client.post(
        f"/api/v1/projects/{s['order']}/lines",
        json={
            "product_id": s["a"].product_id,
            "quantity": 10,
            "material": "PETG",
        },
    )
    assert response.status_code == 200, response.text
    other = next(line["id"] for line in response.json()["lines"] if line["id"] != s["line"])
    await archive(db_session, {**s, "line": other}, status="completed", quantities=(33, 10))
    before = await state(committing_client, s)
    lines = {line["line_id"]: line for line in before["lines"]}
    request = {
        "line_id": s["line"],
        **({"receive": 10} if receipt == "main" else {"parts": [{"part_id": s["a"].id, "receive": 3}]}),
    }
    response = await committing_client.post(f"/api/v1/projects/{s['order']}/fulfilment", json={"lines": [request]})
    assert response.status_code == 409, response.text
    assert lines[s["line"]]["can_receive"] == 0
    assert lines[s["line"]]["parts"][0]["can_receive"] == 0
    assert lines[other]["can_receive"] == 10
    assert lines[other]["parts"][0]["can_receive"] == 3
    assert await state(committing_client, s) == before


async def test_mixed_stock_receipts_and_later_delivery_do_not_repeat_extras(committing_client, db_session, shop):
    s = await extra_order(committing_client, db_session, shop, a=10, b=0)
    await part_stock.move(db_session, part_id=s["a"].id, delta=33, reason="manual", note="Synthetic A delivery")
    await db_session.commit()
    assert (await committing_client.post(f"/api/v1/projects/{s['order']}/take-stock", json={})).status_code == 200
    await archive(db_session, s, status="completed", quantities=(0, 10))
    for main, extra in ((4, 1), (6, 2)):
        response = await committing_client.post(
            f"/api/v1/projects/{s['order']}/fulfilment",
            json={
                "lines": [{"line_id": s["line"], "receive": main, "parts": [{"part_id": s["a"].id, "receive": extra}]}],
            },
        )
        assert response.status_code == 200, response.text
    for part, qty in ((s["a"], 33), (s["b"], 10)):
        await part_stock.move(db_session, part_id=part.id, delta=qty, reason="manual", note="Synthetic later delivery")
    await db_session.commit()
    for _ in range(2):
        response = await committing_client.post(f"/api/v1/projects/{s['order']}/take-stock", json={})
        assert response.status_code == 200 and response.json()["results"] == []
        assert await part_stock.balances(db_session, s["a"].product_id) == {s["a"].id: 33, s["b"].id: 10}
