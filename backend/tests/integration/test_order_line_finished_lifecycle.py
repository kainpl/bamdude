"""A line's ready units through the order's life (spec workshop-add-to-order, rules 6–8, 13–14)."""

import pytest
from sqlalchemy import select

from backend.app.models.customer import Customer
from backend.app.models.finished_stock import StockItem, StockItemMovement
from backend.app.models.project import Project
from backend.app.services import finished_stock
from backend.tests.fixtures.order_fulfilment import complete_order
from backend.tests.integration.test_order_lines_batch import farm  # noqa: F401 — the shared fixture

pytestmark = pytest.mark.integration


async def _order(db, *, customer=True):
    acme = Customer(name="Acme") if customer else None
    if acme:
        db.add(acme)
        await db.flush()
    order = Project(name="O", customer_id=acme.id if acme else None)
    db.add(order)
    await db.commit()
    return order, acme


async def _add(client, order, product_id, quantity=4, **extra):
    r = await client.post(
        f"/api/v1/projects/{order.id}/lines/batch",
        json={"lines": [{"kind": "product", "product_id": product_id, "quantity": quantity, "stock": "auto", **extra}]},
    )
    assert r.status_code == 200, r.text
    return r.json()["order"]["lines"][-1]


async def _line(client, order, line_id):
    lines = (await client.get(f"/api/v1/projects/{order.id}")).json()["lines"]
    return next(line for line in lines if line["id"] == line_id)


async def _standard(db, farm):
    """The standard Pipe position — the first one the fixture created."""
    # populate_existing, never expire_all: an expired fixture attribute would lazy-load.
    return (
        await db.execute(
            select(StockItem)
            .where(StockItem.product_id == farm["pipe"].id)
            .order_by(StockItem.id)
            .limit(1)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _complete(client, order):
    r = await complete_order(client, order.id)
    assert r.status_code == 200, r.text


async def _set_status(client, order, status):
    r = await client.patch(f"/api/v1/projects/{order.id}", json={"status": status})
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_the_figures_count_ready_units_as_covered(committing_client, db_session, farm):
    order, _ = await _order(db_session)
    line = await _add(committing_client, order, farm["pipe"].id)
    assert (line["from_finished"], line["from_kit_units"], line["from_stock_units"]) == (2, 2, 4)
    assert line["covered_units"] == 4
    assert all(part["need"] == 0 for part in line["parts"])  # nothing left to print


@pytest.mark.asyncio
async def test_completing_ships_the_ready_units_to_the_customer(committing_client, db_session, farm):
    order, acme = await _order(db_session)
    line = await _add(committing_client, order, farm["pipe"].id)
    await _complete(committing_client, order)
    issue = (await db_session.execute(select(StockItemMovement).where(StockItemMovement.kind == "issue"))).scalar_one()
    assert (issue.customer_id, issue.project_line_id, issue.delta_on_hand, issue.delta_reserved) == (
        acme.id,
        line["id"],
        -4,
        -4,
    )
    standard = await _standard(db_session, farm)
    assert (standard.on_hand, standard.reserved) == (0, 0)
    after = await _line(committing_client, order, line["id"])
    assert after["from_finished"] == 2 and after["covered_units"] == line["covered_units"]


@pytest.mark.asyncio
async def test_an_order_without_a_customer_issues_nothing(committing_client, db_session, farm):
    """An issue names its customer (spec workshop-order-issue, rule 16) — the WS-10 silent
    issue to nobody at completion is gone."""
    order, _ = await _order(db_session, customer=False)
    line = await _add(committing_client, order, farm["pipe"].id)
    body = {"lines": [{"line_id": line["id"], "assemble": 2, "issue": 4}], "complete": True}
    r = await committing_client.post(f"/api/v1/projects/{order.id}/fulfilment", json=body)
    assert r.status_code == 409
    assert r.json()["detail"] == "An issue names its customer — set the order's customer first"
    assert (await db_session.execute(select(StockItemMovement).where(StockItemMovement.kind == "issue"))).all() == []


@pytest.mark.asyncio
async def test_cancelling_an_active_order_gives_back_both(committing_client, db_session, farm):
    order, _ = await _order(db_session)
    line = await _add(committing_client, order, farm["pipe"].id)
    await _set_status(committing_client, order, "cancelled")
    standard = await _standard(db_session, farm)
    assert (standard.on_hand, standard.reserved) == (2, 0)
    after = await _line(committing_client, order, line["id"])
    assert (after["from_finished"], after["from_kit_units"]) == (0, 0)


@pytest.mark.asyncio
async def test_deleting_a_line_of_an_active_order_gives_back(committing_client, db_session, farm):
    order, _ = await _order(db_session)
    line = await _add(committing_client, order, farm["pipe"].id)
    r = await committing_client.delete(f"/api/v1/projects/{order.id}/lines/{line['id']}")
    assert r.status_code == 200, r.text
    standard = await _standard(db_session, farm)
    assert standard.reserved == 0
    ids = (await db_session.execute(select(StockItemMovement.project_line_id))).scalars().all()
    assert all(i is None for i in ids)


@pytest.mark.asyncio
async def test_deleting_a_line_of_a_completed_order_gives_back_nothing(committing_client, db_session, farm):
    order, _ = await _order(db_session)
    line = await _add(committing_client, order, farm["pipe"].id)
    await _complete(committing_client, order)
    r = await committing_client.delete(f"/api/v1/projects/{order.id}/lines/{line['id']}")
    assert r.status_code == 200, r.text
    standard = await _standard(db_session, farm)
    assert (standard.on_hand, standard.reserved) == (0, 0)  # shipped, not back


@pytest.mark.asyncio
async def test_deleting_an_order_detaches_its_rows(committing_client, db_session, farm):
    order, _ = await _order(db_session)
    await _add(committing_client, order, farm["pipe"].id)
    r = await committing_client.delete(f"/api/v1/projects/{order.id}")
    assert r.status_code == 200, r.text
    rows = (await db_session.execute(select(StockItemMovement.project_id, StockItemMovement.project_line_id))).all()
    assert rows and all(row == (None, None) for row in rows)
    assert (await _standard(db_session, farm)).reserved == 0


@pytest.mark.asyncio
async def test_a_configuration_change_moves_the_ready_units(committing_client, db_session, farm):
    order, _ = await _order(db_session)
    line = await _add(committing_client, order, farm["pipe"].id)
    angled = await finished_stock.item_for(
        db_session, farm["pipe"].id, {farm["group"].id: farm["angled"].id}, create=True
    )
    await finished_stock.receive(db_session, angled, 1)
    await db_session.commit()
    url = f"/api/v1/projects/{order.id}/lines/{line['id']}/configuration"
    body = {"choices": {str(farm["group"].id): farm["angled"].id}, "part_counts": {}}
    dry = await committing_client.put(url, json={**body, "dry_run": True})
    assert dry.status_code == 200, dry.text
    assert (dry.json()["finished_before"], dry.json()["finished_after"]) == (2, 1)
    r = await committing_client.put(
        url,
        json={
            **body,
            "expected_revision": line["configuration"]["revision"],
            "review_version": dry.json()["review_version"],
        },
    )
    assert r.status_code == 200, r.text
    assert (await db_session.get(StockItem, angled.id, populate_existing=True)).reserved == 1
    assert (await _standard(db_session, farm)).reserved == 0
    assert (await _line(committing_client, order, line["id"]))["from_finished"] == 1


@pytest.mark.asyncio
async def test_lowering_the_quantity_gives_kits_back_first(committing_client, db_session, farm):
    order, _ = await _order(db_session)
    line = await _add(committing_client, order, farm["pipe"].id)
    url = f"/api/v1/projects/{order.id}/lines/{line['id']}"
    assert (await committing_client.patch(url, json={"quantity": 3})).status_code == 200
    after = await _line(committing_client, order, line["id"])
    assert (after["from_finished"], after["from_kit_units"]) == (2, 1)
    assert (await committing_client.patch(url, json={"quantity": 1})).status_code == 200
    after = await _line(committing_client, order, line["id"])
    assert (after["from_finished"], after["from_kit_units"]) == (1, 0)
    assert (await _standard(db_session, farm)).reserved == 1


@pytest.mark.asyncio
async def test_patch_rewrites_the_ready_units_and_only_on_an_active_order(committing_client, db_session, farm):
    order, _ = await _order(db_session)
    line = await _add(committing_client, order, farm["pipe"].id)
    url = f"/api/v1/projects/{order.id}/lines/{line['id']}"
    assert (await committing_client.patch(url, json={"from_finished": 1})).status_code == 200
    after = await _line(committing_client, order, line["id"])
    assert after["from_finished"] == 1 and (await _standard(db_session, farm)).reserved == 1
    await _set_status(committing_client, order, "cancelled")
    r = await committing_client.patch(url, json={"from_finished": 1})
    assert r.status_code == 409
    assert r.json()["detail"] == "Only an active order takes finished goods from stock"


@pytest.mark.asyncio
async def test_a_reactivated_completed_order_no_longer_rewrites_its_ready_units(committing_client, db_session, farm):
    """WS-10's «the number is the line's total» (final review I2) is over for a line whose
    units went out through the issue dialog: its stock has moved (spec workshop-order-issue,
    rule 13), so more is only ever TAKEN from stock."""
    order, _ = await _order(db_session)
    line = await _add(committing_client, order, farm["pipe"].id, quantity=2)
    assert line["from_finished"] == 2
    await _complete(committing_client, order)
    await _set_status(committing_client, order, "active")
    url = f"/api/v1/projects/{order.id}/lines/{line['id']}"
    r = await committing_client.patch(url, json={"from_finished": 1})
    assert (r.status_code, r.json()["detail"]) == (409, "This line's stock has moved; take more from stock instead")
    assert (await _line(committing_client, order, line["id"]))["from_finished"] == 2


@pytest.mark.asyncio
async def test_lowering_the_quantity_with_kits_fits_the_ready_units_too(committing_client, db_session, farm):
    """Final review I3: a quantity drop sent together with a kits number still fits the
    ready units under the new quantity."""
    order, _ = await _order(db_session)
    line = await _add(committing_client, order, farm["pipe"].id)
    url = f"/api/v1/projects/{order.id}/lines/{line['id']}"
    r = await committing_client.patch(url, json={"quantity": 1, "from_stock_units": 0})
    assert r.status_code == 200, r.text
    after = await _line(committing_client, order, line["id"])
    assert (after["from_finished"], after["from_kit_units"], after["from_stock_units"]) == (1, 0, 1)
    assert (await _standard(db_session, farm)).reserved == 1


@pytest.mark.asyncio
async def test_a_line_is_never_lowered_under_what_it_issued(committing_client, db_session, farm):
    """WS-10's «lowered under what it shipped» (final review M9) cannot happen any more: the
    quantity stays at or above what is issued and held (spec workshop-order-issue, rule 13)."""
    order, _ = await _order(db_session)
    line = await _add(committing_client, order, farm["pipe"].id, quantity=2)
    await _complete(committing_client, order)
    url = f"/api/v1/projects/{order.id}/lines/{line['id']}"
    r = await committing_client.patch(url, json={"quantity": 1})
    assert r.status_code == 409
    assert r.json()["detail"] == "The quantity cannot go below what is issued and held for this order (2)"


@pytest.mark.asyncio
async def test_a_new_parts_line_refuses_ready_units_as_its_patch_does(committing_client, db_session, farm):
    """Final review M8: creation and PATCH agree — a parts line takes nothing from stock."""
    order, _ = await _order(db_session)
    part = farm["parts"]["flask"]
    body = {"product_id": farm["pipe"].id, "mode": "parts", "part_counts": {str(part.id): 1}, "from_finished": 1}
    r = await committing_client.post(f"/api/v1/projects/{order.id}/lines", json=body)
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "A parts line takes nothing from the shelf"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "cancelled"])
async def test_closing_an_order_locks_its_positions_in_one_order(
    committing_client, db_session, farm, monkeypatch, status
):
    """Final review M4: completion and cancel lock every position the order's lines hold
    in ascending id order, before any line is handled — two orders sharing positions
    cannot lock them in opposite orders (a PostgreSQL deadlock)."""
    order, _ = await _order(db_session)
    await _add(committing_client, order, farm["lamp"].id, quantity=1)  # the later position
    await _add(committing_client, order, farm["pipe"].id, quantity=2)  # the earlier one
    seen: list[int] = []
    original = finished_stock.lock_item

    async def recording(db, item_id):
        if item_id not in seen:
            seen.append(item_id)
        return await original(db, item_id)

    monkeypatch.setattr(finished_stock, "lock_item", recording)
    if status == "completed":
        await _complete(committing_client, order)
    else:
        await _set_status(committing_client, order, status)
    assert len(seen) == 2 and seen == sorted(seen)
