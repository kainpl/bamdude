"""Synthetic regressions from the stock-aware plan review."""

import pytest
from sqlalchemy import func, select

from backend.app.models.finished_stock import StockItemMovement
from backend.app.models.part_stock import ProductPartStockMovement
from backend.app.models.product import ProductPart
from backend.app.models.project import ProjectEvent
from backend.app.models.project_line import ProjectLine
from backend.app.services import part_stock
from backend.tests.integration.test_workshop_stock_plan_regressions import (
    archive,
    shop as shop,
    take_loose_a,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_mixed_coverage_reaches_complete(committing_client, db_session, shop):
    await take_loose_a(committing_client, db_session, shop)
    await archive(db_session, shop, status="completed", quantities=(0, 10))
    response = await committing_client.get(f"/api/v1/projects/{shop['order']}")
    assert response.status_code == 200, response.text
    figures = response.json()["figures"]
    assert figures["covered_units"] == 10
    assert figures["complete"] == 10


async def test_fulfilment_does_not_receive_petg_as_pla(committing_client, db_session, shop):
    first = await committing_client.patch(
        f"/api/v1/projects/{shop['order']}/lines/{shop['line']}",
        json={"material": "PLA"},
    )
    assert first.status_code == 200, first.text
    second = await committing_client.post(
        f"/api/v1/projects/{shop['order']}/lines",
        json={"product_id": shop["a"].product_id, "quantity": 10, "material": "PETG"},
    )
    assert second.status_code == 200, second.text
    other_id = next(line["id"] for line in second.json()["lines"] if line["id"] != shop["line"])
    await archive(db_session, {**shop, "line": other_id}, status="completed", quantities=(30, 10))
    response = await committing_client.get(f"/api/v1/projects/{shop['order']}/fulfilment")
    assert response.status_code == 200, response.text
    can_receive = {line["line_id"]: line["can_receive"] for line in response.json()["lines"]}
    before = response.json()
    models = (StockItemMovement, ProductPartStockMovement, ProjectEvent)
    counts_before = [await db_session.scalar(select(func.count()).select_from(model)) for model in models]
    wrong_receipt = await committing_client.post(
        f"/api/v1/projects/{shop['order']}/fulfilment",
        json={"lines": [{"line_id": shop["line"], "receive": 10}]},
    )
    assert wrong_receipt.status_code == 409, wrong_receipt.text
    assert can_receive[shop["line"]] == 0
    assert can_receive[other_id] == 10
    assert (await committing_client.get(f"/api/v1/projects/{shop['order']}/fulfilment")).json() == before
    assert [await db_session.scalar(select(func.count()).select_from(model)) for model in models] == counts_before
    accepted = await committing_client.post(
        f"/api/v1/projects/{shop['order']}/fulfilment",
        json={"lines": [{"line_id": other_id, "receive": 10}]},
    )
    assert accepted.status_code == 200, accepted.text


async def test_taking_later_stock_does_not_reserve_the_same_a_twice(committing_client, db_session, shop):
    await take_loose_a(committing_client, db_session, shop)
    for part, count in ((shop["a"], 30), (shop["b"], 10)):
        await part_stock.move(db_session, part_id=part.id, delta=count, reason="manual", note="Synthetic later supply")
    await db_session.commit()
    response = await committing_client.post(f"/api/v1/projects/{shop['order']}/take-stock", json={})
    assert response.status_code == 200, response.text
    line = await db_session.get(ProjectLine, shop["line"])
    held = await part_stock.reserved_parts_for_line(db_session, line)
    assert held[shop["a"].id] == 30
    assert held[shop["b"].id] == 10
    assert await part_stock.balances(db_session, shop["a"].product_id) == {shop["a"].id: 30, shop["b"].id: 0}
    repeat = await committing_client.post(f"/api/v1/projects/{shop['order']}/take-stock", json={})
    assert repeat.status_code == 200 and repeat.json()["results"] == []


@pytest.mark.parametrize("already_a, expected_kits", [(15, 5), (30, 0)])
async def test_kit_writer_respects_partial_reservations(committing_client, db_session, shop, already_a, expected_kits):
    await take_loose_a(committing_client, db_session, shop, qty=already_a)
    for part, qty in ((shop["a"], 30), (shop["b"], 10)):
        await part_stock.move(db_session, part_id=part.id, delta=qty, reason="manual", note="Synthetic next delivery")
    line = await db_session.get(ProjectLine, shop["line"])
    assert await part_stock.add_kits_for_line(db_session, line, 10, created_by=None) == expected_kits
    assert await part_stock.reserved_parts_for_line(db_session, line) == {
        shop["a"].id: already_a + 3 * expected_kits,
        **({shop["b"].id: expected_kits} if expected_kits else {}),
    }
    assert await part_stock.balances(db_session, shop["a"].product_id) == {
        shop["a"].id: 30 - 3 * expected_kits,
        shop["b"].id: 10 - expected_kits,
    }


async def test_mixed_receipts_in_batches_remain_complete_and_do_not_receive_twice(committing_client, db_session, shop):
    await take_loose_a(committing_client, db_session, shop)
    await archive(db_session, shop, status="completed", quantities=(0, 10))
    url = f"/api/v1/projects/{shop['order']}/fulfilment"
    for qty, expected_left in ((4, 6), (6, 0)):
        response = await committing_client.post(url, json={"lines": [{"line_id": shop["line"], "receive": qty}]})
        assert response.status_code == 200, response.text
        assert (await committing_client.get(url)).json()["lines"][0]["can_receive"] == expected_left
        assert response.json()["order"]["figures"]["complete"] == 10
    assert (
        await committing_client.post(url, json={"lines": [{"line_id": shop["line"], "receive": 1}]})
    ).status_code == 409
    assert await part_stock.balances(db_session, shop["a"].product_id) == {shop["a"].id: 0, shop["b"].id: 0}


async def test_purchased_parts_gate_mixed_completeness(committing_client, db_session, shop):
    screw = ProductPart(
        product_id=shop["a"].product_id, kind="purchased", name="Part C", name_key="purchased:part c", qty_per_unit=2
    )
    db_session.add(screw)
    await db_session.commit()
    url = f"/api/v1/projects/{shop['order']}/lines/{shop['line']}"
    preview = await committing_client.get(url + "/part-candidates")
    assert preview.status_code == 200, preview.text
    candidates = preview.json()
    added = await committing_client.post(
        url + "/parts",
        json={
            "part_id": screw.id,
            "part_identity": screw.composition_identity,
            "quantity": 2,
            "expected_revision": candidates["revision"],
            "expected_quantity": candidates["quantity"],
            "catalog_version": candidates["catalog_version"],
            "command_id": "purchased-part-required",
        },
    )
    assert added.status_code == 200, added.text
    await take_loose_a(committing_client, db_session, shop)
    await archive(db_session, shop, status="completed", quantities=(0, 10))
    response = await committing_client.get(f"/api/v1/projects/{shop['order']}")
    assert response.json()["figures"]["covered_units"] == 10
    assert response.json()["figures"]["complete"] == 0


async def test_loose_stock_journal_names_are_snapshots(committing_client, db_session, shop):
    await take_loose_a(committing_client, db_session, shop)
    event = await db_session.scalar(select(ProjectEvent).where(ProjectEvent.kind == "stock_taken"))
    assert event.payload["parts_taken"] == [["Part A", 30]]
    shop["a"].name = "Renamed A"
    await db_session.commit()
    await db_session.refresh(event)
    assert event.payload["parts_taken"] == [["Part A", 30]]
