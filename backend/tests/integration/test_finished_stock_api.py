"""Finished goods over the API (spec workshop-finished-goods, rules 16–22)."""

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.core.auth import create_access_token, generate_api_key
from backend.app.models.api_key import APIKey
from backend.app.models.customer import Customer
from backend.app.models.finished_stock import StockItemMovement
from backend.app.models.product import Product, ProductOrigin, ProductPart
from backend.app.models.product_variant import ProductVariantGroup, ProductVariantOption
from backend.app.models.user import User
from backend.app.services import part_stock
from backend.tests.auth_helpers import bind_test_key, mint_test_access

pytestmark = pytest.mark.integration

_PW = "Str0ng-Passw0rd!"


@pytest.fixture
async def farm(db_session):
    """Pipe (Tail: straight standard / angled) and Lamp (no variants, SKU LMP-1)."""
    pipe = Product(name="Pipe")
    lamp = Product(name="Lamp", sku="LMP-1", sku_key="lmp-1")
    db_session.add_all([pipe, lamp])
    await db_session.flush()
    group = ProductVariantGroup(product_id=pipe.id, name="Tail")
    db_session.add(group)
    await db_session.flush()
    straight = ProductVariantOption(group_id=group.id, name="straight", position=0)
    angled = ProductVariantOption(group_id=group.id, name="angled", position=1)
    db_session.add_all([straight, angled])
    await db_session.flush()
    group.default_option_id = straight.id
    parts = {
        "flask": ProductPart(product_id=pipe.id, kind="printed", name="flask", name_key="flask", qty_per_unit=1),
        "straight": ProductPart(
            product_id=pipe.id,
            kind="printed",
            name="straight",
            name_key="straight",
            qty_per_unit=1,
            variant_option_id=straight.id,
        ),
        "angled": ProductPart(
            product_id=pipe.id,
            kind="printed",
            name="angled",
            name_key="angled",
            qty_per_unit=1,
            variant_option_id=angled.id,
        ),
        "shade": ProductPart(product_id=lamp.id, kind="printed", name="shade", name_key="shade", qty_per_unit=1),
    }
    db_session.add_all(parts.values())
    await db_session.flush()
    for name, n in (("flask", 4), ("straight", 3), ("angled", 1), ("shade", 2)):
        await part_stock.move(db_session, part_id=parts[name].id, delta=n, reason="manual", note="seed")
    await db_session.commit()
    return {"pipe": pipe, "lamp": lamp, "group": group, "straight": straight, "angled": angled, "parts": parts}


async def _move(client, **body):
    r = await client.post("/api/v1/stock/moves", json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def _positions(client, farm):
    """Pipe standard 5 (minimum 10 — low), Pipe angled 2 all reserved, Lamp 1."""
    standard = await _move(client, kind="receipt", product_id=farm["pipe"].id, qty=5)
    r = await client.patch(f"/api/v1/stock/items/{standard['id']}", json={"min_qty": 10, "location": "B-02"})
    assert r.status_code == 200, r.text
    angled = await _move(client, kind="receipt", product_id=farm["pipe"].id, options=[farm["angled"].id], qty=2)
    await _move(client, kind="reserve", item_id=angled["id"], qty=2, note="showroom")
    lamp = await _move(client, kind="receipt", product_id=farm["lamp"].id, qty=1)
    return standard, angled, lamp


@pytest.mark.asyncio
async def test_a_receipt_by_product_and_options_creates_a_coded_position(committing_client, farm):
    body = await _move(
        committing_client, kind="receipt", product_id=farm["pipe"].id, options=[farm["angled"].id], qty=3, note="in"
    )
    assert body["code"] == f"SK-{body['id']:04d}"
    assert (body["on_hand"], body["reserved"], body["available"]) == (3, 0, 3)
    choice = body["configuration"]["choices"][0]
    assert (choice["option_name"], choice["is_default"]) == ("angled", False)
    assert body["product"]["name"] == "Pipe"
    again = await _move(
        committing_client, kind="receipt", product_id=farm["pipe"].id, options=[farm["angled"].id], qty=1
    )
    assert again["id"] == body["id"] and again["on_hand"] == 4


@pytest.mark.asyncio
async def test_list_modes_search_sort_and_paging(committing_client, farm):
    standard, angled, lamp = await _positions(committing_client, farm)

    async def ids(**params):
        r = await committing_client.get("/api/v1/stock/items", params={"page": 1, **params})
        assert r.status_code == 200, r.text
        return [row["id"] for row in r.json()["items"]]

    assert sorted(await ids()) == sorted([standard["id"], angled["id"], lamp["id"]])
    assert await ids(mode="low") == [standard["id"]]
    assert await ids(mode="reserved") == [angled["id"]]
    assert await ids(q="angled") == [angled["id"]]
    assert await ids(q="lmp") == [lamp["id"]]
    assert await ids(q=lamp["code"]) == [lamp["id"]]
    assert await ids(q="b-02") == [standard["id"]]
    assert await ids(q="pipe angled") == [angled["id"]]
    assert await ids(sort_by="available-asc") == [angled["id"], lamp["id"], standard["id"]]
    page = (await committing_client.get("/api/v1/stock/items", params={"page": 1, "per_page": 2})).json()
    assert len(page["items"]) == 2 and page["meta"]["total"] == 3 and page["meta"]["last_page"] == 2
    row = (
        next(r for r in page["items"] if r["id"] == standard["id"])
        if any(r["id"] == standard["id"] for r in page["items"])
        else None
    )
    if row:
        assert row["below_min"] is True and row["location"] == "B-02"


@pytest.mark.asyncio
async def test_can_assemble_reads_the_shelf_once_per_page(committing_client, farm, monkeypatch):
    await _positions(committing_client, farm)
    calls = {"batch": 0, "single": 0}
    batch, single = part_stock.balances_for_products, part_stock.balances

    async def counting_batch(*a, **k):
        calls["batch"] += 1
        return await batch(*a, **k)

    async def counting_single(*a, **k):
        calls["single"] += 1
        return await single(*a, **k)

    monkeypatch.setattr(part_stock, "balances_for_products", counting_batch)
    monkeypatch.setattr(part_stock, "balances", counting_single)
    rows = (await committing_client.get("/api/v1/stock/items", params={"page": 1})).json()["items"]
    assert calls == {"batch": 1, "single": 0}
    by_name = {
        (
            r["product"]["name"],
            r["configuration"]["choices"][0]["option_name"] if r["configuration"]["choices"] else "",
        ): r
        for r in rows
    }
    assert by_name[("Pipe", "straight")]["can_assemble"] == 3  # flask 4, straight 3
    assert by_name[("Pipe", "angled")]["can_assemble"] == 1
    assert by_name[("Lamp", "")]["can_assemble"] == 2


@pytest.mark.asyncio
async def test_the_summary_is_the_farm(committing_client, farm):
    await _positions(committing_client, farm)
    body = (await committing_client.get("/api/v1/stock/items/summary", params={"q": "lamp"})).json()
    assert body == {"on_hand": 8, "reserved": 2, "available": 6, "tracked": 3, "below_min": 1}


@pytest.mark.asyncio
async def test_a_position_page(committing_client, farm):
    standard, angled, _lamp = await _positions(committing_client, farm)
    body = (await committing_client.get(f"/api/v1/stock/items/{angled['id']}")).json()
    assert body["reservations"] == [
        {"project_line_id": None, "project_id": None, "project_code": None, "project_name": None, "qty": 2}
    ]
    assert [s["id"] for s in body["siblings"]] == [standard["id"]]
    assert {p["name"]: (p["per"], p["on_shelf"]) for p in body["parts"]} == {"flask": (1, 4), "angled": (1, 1)}
    assert body["can_assemble"] == 1
    missing = await committing_client.get("/api/v1/stock/items/999999")
    assert missing.status_code == 404 and missing.json()["detail"] == "Stock position not found"


@pytest.mark.asyncio
async def test_lookup_of_a_configuration_without_a_position(committing_client, farm):
    body = (
        await committing_client.get(
            "/api/v1/stock/items/lookup", params={"product_id": farm["pipe"].id, "options": str(farm["angled"].id)}
        )
    ).json()
    assert body["item"] is None
    assert body["configuration"]["choices"][0]["option_name"] == "angled"
    assert body["can_assemble"] == 1
    # The assembly dialog draws the kit from the answer: part, per unit, on the shelf.
    assert [(p["name"], p["per"], p["on_shelf"]) for p in body["parts"]] == [("flask", 1, 4), ("angled", 1, 1)]
    standard = await _move(committing_client, kind="receipt", product_id=farm["pipe"].id, qty=1)
    known = (await committing_client.get("/api/v1/stock/items/lookup", params={"product_id": farm["pipe"].id})).json()
    assert known["item"]["id"] == standard["id"]
    assert [(p["name"], p["on_shelf"]) for p in known["parts"]] == [("flask", 4), ("straight", 3)]
    bad = await committing_client.get(
        "/api/v1/stock/items/lookup", params={"product_id": farm["pipe"].id, "options": "999999"}
    )
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_assembly_over_the_api(committing_client, farm):
    r = await committing_client.post(
        "/api/v1/stock/assemble", json={"product_id": farm["pipe"].id, "options": [], "qty": 2, "note": "batch"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["on_hand"] == 2 and r.json()["can_assemble"] == 1
    r = await committing_client.post("/api/v1/stock/assemble", json={"item_id": r.json()["id"], "qty": 5})
    assert r.status_code == 409
    assert r.json()["detail"] == "Only 1 can be assembled from the free parts"


@pytest.mark.asyncio
async def test_refusals_map_to_statuses(committing_client, db_session, farm):
    lamp = await _move(committing_client, kind="receipt", product_id=farm["lamp"].id, qty=1)
    r = await committing_client.post("/api/v1/stock/moves", json={"kind": "reserve", "item_id": lamp["id"], "qty": 2})
    assert (r.status_code, r.json()["detail"]) == (409, "Only 1 available")
    r = await committing_client.post(
        "/api/v1/stock/moves", json={"kind": "stocktake", "item_id": lamp["id"], "counted": 0}
    )
    assert (r.status_code, r.json()["detail"]) == (422, "A lower count needs a note")
    r = await committing_client.post("/api/v1/stock/moves", json={"kind": "issue", "item_id": 999999, "qty": 1})
    assert (r.status_code, r.json()["detail"]) == (404, "Stock position not found")
    r = await committing_client.post(
        "/api/v1/stock/moves", json={"kind": "reserve", "product_id": farm["lamp"].id, "qty": 1, "options": []}
    )
    assert r.status_code == 200  # the lamp's position exists — found by product
    r = await committing_client.post(
        "/api/v1/stock/moves", json={"kind": "receipt", "product_id": farm["pipe"].id, "options": [999999], "qty": 1}
    )
    assert r.status_code == 422
    r = await committing_client.post(
        "/api/v1/stock/moves", json={"kind": "issue", "item_id": lamp["id"], "qty": 1, "customer_id": 999999}
    )
    assert (r.status_code, r.json()["detail"]) == (404, "Customer not found")
    job = Product(name="Job", origin=ProductOrigin.ADHOC_JOB.value)
    db_session.add(job)
    await db_session.commit()
    r = await committing_client.post("/api/v1/stock/moves", json={"kind": "receipt", "product_id": job.id, "qty": 1})
    assert (r.status_code, r.json()["detail"]) == (422, "Only catalogue products are kept in stock")


@pytest.mark.asyncio
async def test_an_issue_names_the_customer(committing_client, db_session, farm):
    customer = Customer(name="Acme")
    db_session.add(customer)
    await db_session.commit()
    lamp = await _move(committing_client, kind="receipt", product_id=farm["lamp"].id, qty=2)
    body = await _move(committing_client, kind="issue", item_id=lamp["id"], qty=1, customer_id=customer.id)
    assert body["on_hand"] == 1


@pytest.mark.asyncio
async def test_params_and_the_sidebar_badge(committing_client, farm):
    standard, _angled, _lamp = await _positions(committing_client, farm)
    assert standard["location"] is None
    badge = (await committing_client.get("/api/v1/projects/nav-badges")).json()
    assert badge["stock_below_min"] == 1
    r = await committing_client.patch(f"/api/v1/stock/items/{standard['id']}", json={"min_qty": 0})
    assert r.json()["below_min"] is False and r.json()["location"] == "B-02"
    badge = (await committing_client.get("/api/v1/projects/nav-badges")).json()
    assert badge["stock_below_min"] == 0


@pytest.mark.asyncio
async def test_reading_is_not_moving(async_client: AsyncClient, farm):
    admin = {"Authorization": f"Bearer {await mint_test_access(username='test_admin')}"}
    grp = await async_client.post(
        "/api/v1/groups/",
        headers=admin,
        json={"name": "stock_readers", "permissions": ["orders:read", "products:read", "customers:read", "stock:read"]},
    )
    assert grp.status_code == 201, grp.text
    created = await async_client.post(
        "/api/v1/users/",
        headers=admin,
        json={"username": "stock_reader", "password": _PW, "role": "user", "group_ids": [grp.json()["id"]]},
    )
    assert created.status_code == 201, created.text
    login = await async_client.post("/api/v1/auth/login", json={"username": "stock_reader", "password": _PW})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert (await async_client.get("/api/v1/stock/items", params={"page": 1}, headers=headers)).status_code == 200
    r = await async_client.post(
        "/api/v1/stock/moves", headers=headers, json={"kind": "receipt", "product_id": farm["lamp"].id, "qty": 1}
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_deleting_a_customer_detaches_their_issues(committing_client, db_session, farm):
    """SQLite runs no FK actions, and `customers` reuses ids: an issue left pointing at a
    deleted customer would be credited to whoever gets the id next (review Important 3)."""
    acme = Customer(name="Acme")
    db_session.add(acme)
    await db_session.commit()
    lamp = await _move(committing_client, kind="receipt", product_id=farm["lamp"].id, qty=2)
    await _move(committing_client, kind="issue", item_id=lamp["id"], qty=1, customer_id=acme.id)
    assert (await committing_client.delete(f"/api/v1/customers/{acme.id}")).status_code in (200, 204)
    rows = (await committing_client.get("/api/v1/stock/journal", params={"book": "finished"})).json()["items"]
    issue = next(r for r in rows if r["kind"] == "issue")
    assert issue["customer"] is None
    customer_ids = (await db_session.execute(select(StockItemMovement.customer_id))).scalars().all()
    assert acme.id not in customer_ids


@pytest.mark.asyncio
async def test_an_api_key_records_its_owner_as_the_performer(committing_client, db_session, farm):
    """Spec rule 12: for an API key the performer is its owner — not NULL, which reads as
    «the system» (review Important 5)."""
    admin = (await db_session.execute(select(User).where(User.username == "test_admin"))).scalar_one()
    full_key, key_hash, key_prefix = generate_api_key()
    issued_key = APIKey(
        name="stock-bridge",
        key_hash=key_hash,
        key_prefix=key_prefix,
        user_id=admin.id,
        can_manage_projects=True,
        can_read_status=True,
    )
    await bind_test_key(db_session, issued_key)
    db_session.add(issued_key)
    await db_session.commit()
    jwt = committing_client.headers.pop("Authorization")
    try:
        r = await committing_client.post(
            "/api/v1/stock/moves",
            headers={"X-API-Key": full_key},
            json={"kind": "receipt", "product_id": farm["lamp"].id, "qty": 1},
        )
    finally:
        committing_client.headers["Authorization"] = jwt
    assert r.status_code == 200, r.text
    rows = (await committing_client.get("/api/v1/stock/journal", params={"book": "finished"})).json()["items"]
    assert rows[0]["user"] == {"id": admin.id, "username": "test_admin"}


@pytest.mark.asyncio
async def test_a_zero_count_of_a_configuration_without_a_position_creates_nothing(committing_client, farm):
    """A position appears with its first movement; a count of 0 moves nothing, so an
    empty position must not be left behind to hold its option forever (review Minor 6)."""
    r = await committing_client.post(
        "/api/v1/stock/moves", json={"kind": "stocktake", "product_id": farm["lamp"].id, "counted": 0}
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "Stock position not found"
    page = (await committing_client.get("/api/v1/stock/items", params={"mode": "all", "page": 1})).json()
    assert page["meta"]["total"] == 0


@pytest.mark.asyncio
async def test_a_count_that_matches_the_shelf_says_nothing_moved(committing_client, farm):
    """Spec rule 10: no difference — no movement, and the answer says so (review Minor 7)."""
    lamp = await _move(committing_client, kind="receipt", product_id=farm["lamp"].id, qty=3)
    assert lamp["moved"] is True
    same = await _move(committing_client, kind="stocktake", item_id=lamp["id"], counted=3)
    assert same["moved"] is False and same["on_hand"] == 3
    rows = (await committing_client.get("/api/v1/stock/journal", params={"book": "finished"})).json()["items"]
    assert [r["kind"] for r in rows] == ["receipt"]


@pytest.mark.asyncio
async def test_the_shortfall_is_the_servers(committing_client, farm):
    """Workshop rule: figures come from the server — «short by N» too (review Minor 9)."""
    standard, _angled, lamp = await _positions(committing_client, farm)
    rows = {
        r["id"]: r
        for r in (await committing_client.get("/api/v1/stock/items", params={"mode": "all", "page": 1})).json()["items"]
    }
    assert rows[standard["id"]]["short_by"] == 5
    assert rows[lamp["id"]]["short_by"] == 0
    detail = (await committing_client.get(f"/api/v1/stock/items/{standard['id']}")).json()
    assert detail["short_by"] == 5


@pytest.mark.asyncio
async def test_an_absurd_quantity_is_refused_not_a_server_error(committing_client, farm):
    """An INTEGER column overflows on PostgreSQL — the request must be refused first (review Minor 10)."""
    for body in (
        {"kind": "receipt", "product_id": farm["lamp"].id, "qty": 10**10},
        {"kind": "stocktake", "product_id": farm["lamp"].id, "counted": 10**10},
    ):
        assert (await committing_client.post("/api/v1/stock/moves", json=body)).status_code == 422
    r = await committing_client.post("/api/v1/stock/assemble", json={"product_id": farm["lamp"].id, "qty": 10**10})
    assert r.status_code == 422
    lamp = await _move(committing_client, kind="receipt", product_id=farm["lamp"].id, qty=1)
    r = await committing_client.patch(f"/api/v1/stock/items/{lamp['id']}", json={"min_qty": 10**10})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_two_options_of_one_group_are_refused(committing_client, farm):
    """A position has one option per group; two of the same group is not a choice (review Minor 11)."""
    body = {
        "kind": "receipt",
        "product_id": farm["pipe"].id,
        "options": [farm["straight"].id, farm["angled"].id],
        "qty": 1,
    }
    r = await committing_client.post("/api/v1/stock/moves", json=body)
    assert r.status_code == 422
    assert r.json()["detail"] == "Pick one option per group"
    r = await committing_client.get(
        "/api/v1/stock/items/lookup",
        params={"product_id": farm["pipe"].id, "options": f"{farm['straight'].id},{farm['angled'].id}"},
    )
    assert r.status_code == 422
