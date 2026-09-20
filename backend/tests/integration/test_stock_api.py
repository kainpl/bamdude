"""``GET /stock`` and ``GET /stock/movements`` — the farm-wide shelf.

``committing_client``, not ``async_client``: the handlers never commit;
production's ``get_db`` does it after the response (see the conftest fixture
docstrings). The lamp fixture is the products API's own — five lids and three
bases are three kits, the screw is procurement and never appears.
"""

import pytest

from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.app.services.part_stock import move, release_for_line, reserve_for_line
from backend.tests.integration.test_products_api import _lamp_with_stock


async def _order_with_line(db, product_id: int, *, name: str, status: str = "active") -> ProjectLine:
    project = Project(name=name, status=status)
    db.add(project)
    await db.flush()
    line = ProjectLine(project_id=project.id, product_id=product_id, quantity=5)
    db.add(line)
    await db.flush()
    return line


@pytest.mark.asyncio
async def test_summary_lists_counted_products_with_balances_and_kits(committing_client, db_session):
    pid, ids = await _lamp_with_stock(committing_client, db_session)
    # A product with nothing to count has no shelf and is not a row.
    bare = (await committing_client.post("/api/v1/products/", json={"name": "Bare"})).json()["id"]

    body = (await committing_client.get("/api/v1/stock")).json()
    rows = {p["id"]: p for p in body["products"]}
    assert pid in rows and bare not in rows
    lamp = rows[pid]
    assert lamp["name"] == "Lamp" and lamp["kits_available"] == 3
    assert {(b["name"], b["balance"]) for b in lamp["parts"]} == {("lid", 5), ("base", 3)}
    assert lamp["reservations"] == []


@pytest.mark.asyncio
async def test_with_stock_hides_an_empty_shelf_by_default(committing_client, db_session):
    stocked, _ = await _lamp_with_stock(committing_client, db_session, name="Stocked")
    empty, _ = await _lamp_with_stock(committing_client, db_session, name="Empty", lids=0, bases=0)

    default = {p["id"] for p in (await committing_client.get("/api/v1/stock")).json()["products"]}
    assert stocked in default and empty not in default

    everything = {p["id"] for p in (await committing_client.get("/api/v1/stock?with_stock=false")).json()["products"]}
    assert {stocked, empty} <= everything


@pytest.mark.asyncio
async def test_reservations_come_from_active_orders_only(committing_client, db_session):
    pid, _ = await _lamp_with_stock(committing_client, db_session)
    active = await _order_with_line(db_session, pid, name="Active order")
    done = await _order_with_line(db_session, pid, name="Shipped order", status="completed")
    await reserve_for_line(db_session, active, 2)
    await reserve_for_line(db_session, done, 1)
    await db_session.commit()

    lamp = next(p for p in (await committing_client.get("/api/v1/stock")).json()["products"] if p["id"] == pid)
    assert lamp["reservations"] == [
        {"line_id": active.id, "order_id": active.project_id, "order_name": "Active order", "kits": 2}
    ]
    # The shelf nets both reservations regardless: 5 lids − 3 reserved, 3 bases − 3.
    assert lamp["kits_available"] == 0
    assert {(b["name"], b["balance"]) for b in lamp["parts"]} == {("lid", 2), ("base", 0)}


@pytest.mark.asyncio
async def test_a_reservation_alone_keeps_a_product_on_the_tab(committing_client, db_session):
    """Kits out on loan are still the shelf's business: a product whose whole
    stock is reserved has zero balances and must not vanish with ``with_stock``."""
    pid, _ = await _lamp_with_stock(committing_client, db_session, lids=1, bases=1)
    line = await _order_with_line(db_session, pid, name="Takes it all")
    await reserve_for_line(db_session, line, 1)
    await db_session.commit()

    rows = {p["id"]: p for p in (await committing_client.get("/api/v1/stock")).json()["products"]}
    assert pid in rows and rows[pid]["kits_available"] == 0 and rows[pid]["reservations"][0]["kits"] == 1


@pytest.mark.asyncio
async def test_search_and_order(committing_client, db_session):
    await _lamp_with_stock(committing_client, db_session, name="Zebra lamp", lids=1, bases=1)
    await _lamp_with_stock(committing_client, db_session, name="Apple lamp", lids=1, bases=1)
    await _lamp_with_stock(committing_client, db_session, name="Big lamp", lids=9, bases=9)

    names = [p["name"] for p in (await committing_client.get("/api/v1/stock")).json()["products"]]
    assert names == ["Big lamp", "Apple lamp", "Zebra lamp"], "kits desc, then name asc"

    found = [p["name"] for p in (await committing_client.get("/api/v1/stock?q=ZEB")).json()["products"]]
    assert found == ["Zebra lamp"]


@pytest.mark.asyncio
async def test_search_folds_cyrillic_case(committing_client, db_session):
    # SQLite's built-in lower() is ASCII-only; the app shadows it on every
    # connection (core/case_folding.py). Before that, ?q=ЛАМПА found nothing.
    await _lamp_with_stock(committing_client, db_session, name="Лампа настільна", lids=1, bases=1)
    found = [p["name"] for p in (await committing_client.get("/api/v1/stock?q=ЛАМПА")).json()["products"]]
    assert found == ["Лампа настільна"]


@pytest.mark.asyncio
async def test_journal_is_newest_first_with_product_names_and_pages_by_id(committing_client, db_session):
    pid, ids = await _lamp_with_stock(committing_client, db_session, lids=0, bases=0)
    for delta in (1, 2, 3):
        await move(db_session, part_id=ids["lid"], delta=delta, reason="unfiled_print")
    await db_session.commit()

    first = (await committing_client.get("/api/v1/stock/movements?limit=2")).json()
    assert [r["delta"] for r in first["items"]] == [3, 2]
    assert first["items"][0]["product_id"] == pid and first["items"][0]["product_name"] == "Lamp"
    assert first["items"][0]["part_name"] == "lid"
    assert first["next_before_id"] == first["items"][-1]["id"]

    rest = (await committing_client.get(f"/api/v1/stock/movements?limit=2&before_id={first['next_before_id']}")).json()
    assert [r["delta"] for r in rest["items"]] == [1]
    assert rest["next_before_id"] is None


@pytest.mark.asyncio
async def test_journal_filters_and_resolves_the_order(committing_client, db_session):
    lamp, lamp_ids = await _lamp_with_stock(committing_client, db_session, name="Lamp", lids=4, bases=4)
    vase, _ = await _lamp_with_stock(committing_client, db_session, name="Vase", lids=1, bases=1)
    line = await _order_with_line(db_session, lamp, name="Order 7")
    await reserve_for_line(db_session, line, 1)
    await db_session.commit()

    by_product = (await committing_client.get(f"/api/v1/stock/movements?product_id={lamp}")).json()["items"]
    assert {r["product_id"] for r in by_product} == {lamp}
    by_part = (await committing_client.get(f"/api/v1/stock/movements?part_id={lamp_ids['base']}")).json()["items"]
    assert {r["part_name"] for r in by_part} == {"base"}
    reserved = (await committing_client.get("/api/v1/stock/movements?reason=reserved_for_order")).json()["items"]
    assert reserved and all(r["reason"] == "reserved_for_order" for r in reserved)
    assert reserved[0]["order_id"] == line.project_id and reserved[0]["order_name"] == "Order 7"
    assert vase not in {r["product_id"] for r in reserved}


@pytest.mark.asyncio
async def test_journal_refuses_an_unknown_reason(committing_client):
    r = await committing_client.get("/api/v1/stock/movements?reason=teleported")
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_a_cancelled_orders_released_reservation_is_not_listed(committing_client, db_session):
    pid, _ids = await _lamp_with_stock(committing_client, db_session)
    line = await _order_with_line(db_session, pid, name="Cancel me")
    await reserve_for_line(db_session, line, 2)
    await db_session.commit()

    project = await db_session.get(Project, line.project_id)
    project.status = "cancelled"
    await release_for_line(db_session, line, note="order_cancelled")
    await db_session.commit()

    lamp = next(p for p in (await committing_client.get("/api/v1/stock")).json()["products"] if p["id"] == pid)
    assert lamp["reservations"] == []
    assert lamp["kits_available"] == 3
    assert {(b["name"], b["balance"]) for b in lamp["parts"]} == {("lid", 5), ("base", 3)}


@pytest.mark.asyncio
async def test_kits_are_the_scarcest_part_divided_by_its_per_unit(committing_client, db_session):
    pid = (await committing_client.post("/api/v1/products/", json={"name": "Gadget"})).json()["id"]
    lid = (
        await committing_client.post(
            f"/api/v1/products/{pid}/parts", json={"kind": "printed", "name": "lid", "qty_per_unit": 2}
        )
    ).json()["id"]
    base = (
        await committing_client.post(
            f"/api/v1/products/{pid}/parts", json={"kind": "printed", "name": "base", "qty_per_unit": 1}
        )
    ).json()["id"]
    await move(db_session, part_id=lid, delta=5, reason="unfiled_print")
    await move(db_session, part_id=base, delta=3, reason="unfiled_print")
    await db_session.commit()

    lamp = next(p for p in (await committing_client.get("/api/v1/stock")).json()["products"] if p["id"] == pid)
    assert lamp["kits_available"] == 2
