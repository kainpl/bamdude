"""The final review of WS-11 (spec workshop-order-issue): what received units must not do."""

import pytest
from sqlalchemy import select, text

from backend.app.core.auth import generate_api_key
from backend.app.models.api_key import APIKey
from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart
from backend.app.models.customer import Customer
from backend.app.models.finished_stock import StockItem, StockItemMovement
from backend.app.models.product import Product, ProductOrigin, ProductPart
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.app.models.stock_issue import StockIssue
from backend.app.models.user import User
from backend.app.services import (
    archive_defects,
    finished_stock,
    line_config,
    order_fulfilment,
    part_stock,
    stock_issue_views,
    stock_offers,
)
from backend.app.services.order_fulfilment import FulfilmentError, LineRequest
from backend.app.services.order_metrics import attribute, load_order_context
from backend.app.services.stock_issues import Recipient
from backend.tests.auth_helpers import bind_test_key
from backend.tests.integration.test_orders_api import _completed_print, catalog  # noqa: F401 — the shared fixture

pytestmark = pytest.mark.integration

LEAVE = "These prints went onto the shelf for the order — they cannot leave it"


async def _lamp_order(client, catalog, quantities):  # noqa: F811 — the fixture's value
    body = {
        "name": "Lamps",
        "customer_id": catalog["customer"].id,
        "lines": [{"product_id": catalog["product"].id, "quantity": q} for q in quantities],
    }
    r = await client.post("/api/v1/projects/", json=body)
    assert r.status_code == 200, r.text
    order = r.json()
    return order["id"], [line["id"] for line in order["lines"]]


async def _fulfil(client, order_id, lines, **extra):
    return await client.post(f"/api/v1/projects/{order_id}/fulfilment", json={"lines": lines, **extra})


async def _free_parts(db, product_id):
    return {pid: n for pid, n in (await part_stock.balances(db, product_id)).items() if n}


# ---------- C1: a received print is not credited to the free shelf a second time ----------


@pytest.mark.asyncio
async def test_deleting_an_order_that_received_its_print_credits_no_parts(committing_client, db_session, catalog):  # noqa: F811
    order_id, [line_id] = await _lamp_order(committing_client, catalog, [1])
    await _completed_print(db_session, order_id, catalog["file"].id)
    assert (await _fulfil(committing_client, order_id, [{"line_id": line_id, "receive": 1}])).status_code == 200
    assert (await committing_client.delete(f"/api/v1/projects/{order_id}")).status_code == 200
    position = (
        await db_session.execute(
            select(StockItem)
            .where(StockItem.product_id == catalog["product"].id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert (position.on_hand, position.reserved) == (1, 0)  # the lamp is free stock now
    assert await _free_parts(db_session, catalog["product"].id) == {}  # and not ALSO a free kit


@pytest.mark.asyncio
async def test_a_received_print_cannot_leave_its_order(committing_client, db_session, catalog):  # noqa: F811
    order_id, [line_id] = await _lamp_order(committing_client, catalog, [1])
    archive = await _completed_print(db_session, order_id, catalog["file"].id)
    assert (await _fulfil(committing_client, order_id, [{"line_id": line_id, "receive": 1}])).status_code == 200
    r = await committing_client.post(f"/api/v1/projects/{order_id}/remove-archives", json={"archive_ids": [archive.id]})
    assert (r.status_code, r.json()["detail"]) == (409, LEAVE)
    r = await committing_client.patch(f"/api/v1/archives/{archive.id}", json={"project_id": None})
    assert (r.status_code, r.json()["detail"]) == (409, LEAVE)
    assert (await db_session.get(PrintArchive, archive.id, populate_existing=True)).project_id == order_id
    assert await _free_parts(db_session, catalog["product"].id) == {}


@pytest.mark.asyncio
async def test_a_print_beyond_what_was_received_may_still_leave(committing_client, db_session, catalog):  # noqa: F811
    order_id, [line_id] = await _lamp_order(committing_client, catalog, [1])
    await _completed_print(db_session, order_id, catalog["file"].id)
    spare = await _completed_print(db_session, order_id, catalog["file"].id)
    assert (await _fulfil(committing_client, order_id, [{"line_id": line_id, "receive": 1}])).status_code == 200
    r = await committing_client.post(f"/api/v1/projects/{order_id}/remove-archives", json={"archive_ids": [spare.id]})
    assert r.status_code == 200, r.text
    assert sorted(await _free_parts(db_session, catalog["product"].id)) != []  # the spare went to the shelf


# ---------- C2: units that move between lines are not received twice ----------


@pytest.mark.asyncio
async def test_printed_units_moving_between_lines_are_not_received_twice(committing_client, db_session, catalog):  # noqa: F811
    order_id, [first, second] = await _lamp_order(committing_client, catalog, [1, 1])
    for _ in range(2):
        await _completed_print(db_session, order_id, catalog["file"].id)
    both = [{"line_id": first, "receive": 1}, {"line_id": second, "receive": 1}]
    assert (await _fulfil(committing_client, order_id, both)).status_code == 200
    r = await committing_client.patch(f"/api/v1/projects/{order_id}/lines/{first}", json={"quantity": 2})
    assert r.status_code == 200, r.text
    state = (await committing_client.get(f"/api/v1/projects/{order_id}/fulfilment")).json()
    assert [row["can_receive"] for row in state["lines"]] == [0, 0]  # two prints, two units received
    r = await _fulfil(committing_client, order_id, [{"line_id": first, "receive": 1}])
    assert (r.status_code, r.json()["detail"]) == (409, "«Lamp»: only 0 can be received")


# ---------- I1: merging a part whose counters have no row on the target ----------


@pytest.fixture
async def pipe(db_session):
    """Pipe (flask ×1, cap ×1), 5 of each free; an active order for Acme."""
    product = Product(name="Pipe")
    acme = Customer(name="Acme")
    db_session.add_all([product, acme])
    await db_session.flush()
    flask = ProductPart(product_id=product.id, kind="printed", name="flask", name_key="flask", qty_per_unit=1)
    cap = ProductPart(product_id=product.id, kind="printed", name="cap", name_key="cap", qty_per_unit=1)
    db_session.add_all([flask, cap])
    await db_session.flush()
    for part in (flask, cap):
        await part_stock.move(db_session, part_id=part.id, delta=5, reason="manual", note="seed")
    order = Project(name="O", customer_id=acme.id)
    db_session.add(order)
    await db_session.commit()
    return {"product": product, "flask": flask, "cap": cap, "order": order, "acme": acme}


async def _line(db, pipe, *, quantity=5, mode="product", counts=None, product=None):
    line = ProjectLine(
        project_id=pipe["order"].id, product_id=(product or pipe["product"]).id, quantity=quantity, mode=mode
    )
    db.add(line)
    await db.flush()
    await line_config.seed_line(db, line, choices=None, counts=counts)
    return line


async def _print(db, pipe, line, **parts):
    archive = PrintArchive(
        project_id=pipe["order"].id,
        project_line_id=line.id,
        filename="pipe",
        file_path="",
        file_size=0,
        status="completed",
        quantity=1,
    )
    db.add(archive)
    await db.flush()
    db.add_all([PrintArchivePart(archive_id=archive.id, name=n, name_key=n, quantity=q) for n, q in parts.items()])
    await db.flush()
    return archive


@pytest.mark.asyncio
async def test_merging_a_part_creates_the_target_counter_row(db_session, pipe):
    flask, cap = pipe["flask"], pipe["cap"]
    line = await _line(db_session, pipe, quantity=1, mode="parts", counts={flask.id: 2, cap.id: 2})
    await part_stock.receive_parts_for_line(db_session, line, {cap.id: 1}, created_by=None)
    await part_stock.repoint(db_session, from_part_id=cap.id, to_part_id=flask.id)
    rows = (await part_stock.line_part_stock(db_session, [line.id]))[line.id]
    assert {pid: (row.received, row.issued, row.returned) for pid, row in rows.items()} == {flask.id: (1, 0, 0)}


# ---------- I4: positions are locked before the lines, the position before the parts ----------


@pytest.mark.asyncio
async def test_a_batch_locks_the_lines_positions_before_the_lines(db_session, pipe, monkeypatch):
    line = await _line(db_session, pipe)
    position = await finished_stock.item_for(db_session, pipe["product"].id, {}, create=True)
    await _print(db_session, pipe, line, flask=1, cap=1)
    await db_session.commit()
    seen: list[tuple[str, int]] = []
    lock_item, lock_line = finished_stock.lock_item, finished_stock.lock_line

    async def item(db, item_id):
        seen.append(("item", item_id))
        return await lock_item(db, item_id)

    async def row(db, the_line):
        seen.append(("line", the_line.id))
        return await lock_line(db, the_line)

    monkeypatch.setattr(finished_stock, "lock_item", item)
    monkeypatch.setattr(finished_stock, "lock_line", row)
    await order_fulfilment.apply(
        db_session,
        pipe["order"],
        [LineRequest(line.id, receive=1)],
        recipient=Recipient(),
        waybill=None,
        note=None,
        complete=False,
        actor=None,
    )
    assert seen.index(("item", position.id)) < seen.index(("line", line.id))


@pytest.mark.asyncio
async def test_assembling_locks_the_position_before_the_parts(db_session, pipe, monkeypatch):
    line = await _line(db_session, pipe)
    assert await part_stock.reserve_for_line(db_session, line, 2) == 2
    # A separate request, as in the app: the setup's locks are not the door's (WS-13 E1 BL2).
    await db_session.commit()
    await finished_stock.item_for(db_session, pipe["product"].id, {}, create=True)
    seen: list[str] = []
    lock_item, lock_parts = finished_stock.lock_item, part_stock.lock_parts

    async def item(db, item_id):
        seen.append("item")
        return await lock_item(db, item_id)

    async def parts(db, the_parts):
        seen.append("parts")
        return await lock_parts(db, the_parts)

    monkeypatch.setattr(finished_stock, "lock_item", item)
    monkeypatch.setattr(part_stock, "lock_parts", parts)
    await finished_stock.assemble_for_line(db_session, line, 1, actor=None)
    assert seen.index("item") < seen.index("parts")


# ---------- M1: the status is read under the locks ----------


@pytest.mark.asyncio
async def test_a_cancel_that_committed_first_refuses_the_batch_and_the_take(db_session, pipe):
    line = await _line(db_session, pipe)
    await _print(db_session, pipe, line, flask=1, cap=1)
    await db_session.commit()
    order = pipe["order"]
    await db_session.execute(text("UPDATE projects SET status = 'cancelled' WHERE id = :id"), {"id": order.id})
    assert order.status == "active"  # what this request read before the cancel
    with pytest.raises(FulfilmentError) as refused:
        await order_fulfilment.apply(
            db_session,
            order,
            [LineRequest(line.id, receive=1)],
            recipient=Recipient(),
            waybill=None,
            note=None,
            complete=False,
            actor=None,
        )
    assert str(refused.value) == "Only an active order can be fulfilled"
    with pytest.raises(stock_offers.StockOfferError) as taken:
        await stock_offers.take(db_session, order, None, actor=None)
    assert str(taken.value) == "Only an active order takes finished goods from stock"


# ---------- M2: the give-back of a cancel is recorded against whoever pressed ----------


@pytest.mark.asyncio
async def test_a_cancel_by_an_api_key_records_its_owner(committing_client, db_session, pipe):
    admin = (await db_session.execute(select(User).where(User.username == "test_admin"))).scalar_one()
    line = await _line(db_session, pipe)
    position = await finished_stock.item_for(db_session, pipe["product"].id, {}, create=True)
    await finished_stock.receive(db_session, position, 2)
    assert await finished_stock.reserve_for_line(db_session, line, 2) == 2
    full_key, key_hash, key_prefix = generate_api_key()
    key = APIKey(name="bridge", key_hash=key_hash, key_prefix=key_prefix, user_id=admin.id, can_manage_projects=True)
    await bind_test_key(db_session, key, admin)
    db_session.add(key)
    await db_session.commit()
    jwt = committing_client.headers.pop("Authorization")
    try:
        r = await committing_client.patch(
            f"/api/v1/projects/{pipe['order'].id}", headers={"X-API-Key": full_key}, json={"status": "cancelled"}
        )
    finally:
        committing_client.headers["Authorization"] = jwt
    assert r.status_code == 200, r.text
    release = (
        await db_session.execute(select(StockItemMovement).where(StockItemMovement.kind == "release"))
    ).scalar_one()
    assert release.created_by == admin.id


# ---------- M3: received units stay covered after defects recorded later ----------


@pytest.mark.asyncio
async def test_received_units_stay_covered_after_defects_recorded_later(db_session, pipe):
    line = await _line(db_session, pipe)
    archive = await _print(db_session, pipe, line, flask=5, cap=5)
    await order_fulfilment.apply(
        db_session,
        pipe["order"],
        [LineRequest(line.id, receive=5)],
        recipient=Recipient(),
        waybill=None,
        note=None,
        complete=False,
        actor=None,
    )
    rows = (
        (await db_session.execute(select(PrintArchivePart).where(PrintArchivePart.archive_id == archive.id)))
        .scalars()
        .all()
    )
    await archive_defects.record_defects(
        db_session, archive, archive_defects.DefectsWrite(parts=tuple((r.id, 2) for r in rows))
    )
    figures, _other = attribute(await load_order_context(db_session, pipe["order"].id))
    figs = figures[line.id]
    assert figs.covered_units == 5  # receiving is the check: what went on the shelf is covered
    assert all(p.remaining == 0 for p in figs.parts)  # the plan asks for nothing more


# ---------- M4: a product without printed parts is received too ----------


@pytest.mark.asyncio
async def test_a_line_of_a_product_without_printed_parts_is_received_and_issued(db_session, pipe):
    kit = Product(name="Screw kit")
    db_session.add(kit)
    await db_session.flush()
    db_session.add(ProductPart(product_id=kit.id, kind="purchased", name="M3", name_key="purchased:m3", qty_per_unit=4))
    await db_session.flush()
    line = await _line(db_session, pipe, quantity=2, product=kit)
    await db_session.commit()
    [row] = (await order_fulfilment.state(db_session, pipe["order"])).lines
    assert row.can_receive == 2
    await order_fulfilment.apply(
        db_session,
        pipe["order"],
        [LineRequest(line.id, receive=2, issue=2)],
        recipient=Recipient(),
        waybill=None,
        note=None,
        complete=True,
        actor=None,
    )
    assert pipe["order"].status == "completed"


# ---------- M5: the two add-only doors share one room ----------


@pytest.mark.asyncio
async def test_the_add_only_doors_never_cover_past_the_quantity(db_session, pipe):
    line = await _line(db_session, pipe)
    position = await finished_stock.item_for(db_session, pipe["product"].id, {}, create=True)
    await finished_stock.receive(db_session, position, 10)
    await finished_stock.produce_for_line(db_session, line, 2, actor=None)
    assert await part_stock.reserve_for_line(db_session, line, 2) == 2
    # 5 ordered: 2 received + 2 kits leave room for 1 — in each door.
    assert await part_stock.add_kits_for_line(db_session, line, 10, created_by=None) == 1
    assert await finished_stock.take_for_line(db_session, line, 10, actor=None) == 0
    other = await _line(db_session, pipe)
    await finished_stock.produce_for_line(db_session, other, 2, actor=None)
    assert await part_stock.reserve_for_line(db_session, other, 2) == 2
    assert await finished_stock.take_for_line(db_session, other, 10, actor=None) == 1


# ---------- a one-off product that went through the shelf keeps its history ----------


@pytest.mark.asyncio
async def test_a_one_off_product_whose_units_were_issued_outlives_its_deleted_order(
    committing_client, db_session, pipe
):
    plate = Product(name="Plate 1 of flask.3mf", origin=ProductOrigin.ADHOC_PLATE.value)
    db_session.add(plate)
    await db_session.flush()
    db_session.add(ProductPart(product_id=plate.id, kind="printed", name="flask", name_key="flask", qty_per_unit=1))
    await db_session.flush()
    line = await _line(db_session, pipe, quantity=2, product=plate)
    await _print(db_session, pipe, line, flask=2)
    await db_session.commit()
    order_id = pipe["order"].id
    body = {"lines": [{"line_id": line.id, "receive": 2, "issue": 2}]}
    r = await committing_client.post(f"/api/v1/projects/{order_id}/fulfilment", json=body)
    assert r.status_code == 200, r.text
    issue_id = r.json()["issue_id"]
    assert (await committing_client.delete(f"/api/v1/projects/{order_id}")).status_code == 200
    assert await db_session.get(Product, plate.id, populate_existing=True) is not None
    issue = await db_session.get(StockIssue, issue_id, populate_existing=True)
    # The dispatch note keeps what went out (spec workshop-dispatch-notes, rule 9).
    [row] = await stock_issue_views.issue_rows(db_session, [issue])
    assert (row.units, row.order_code, row.summary[0].product_name) == (2, f"OR-{order_id:04d}", plate.name)
