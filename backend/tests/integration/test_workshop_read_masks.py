"""What each Workshop read shows of another domain (WS-13 E13 T15, O12).

A response of one domain carries fields of another: an order its contact's phone, a product
its stock and order counts, a customer its orders' money, the sidebar three domains' counts.
Each such field follows the reader's right on ITS domain — null without it — and a filter or a
sort on a field the reader may not see is refused, never silently ignored.
"""

import pytest
from httpx import AsyncClient

from backend.tests.integration.test_workshop_library_rights import _jwt, _user

pytestmark = pytest.mark.integration


async def _desk(client: AsyncClient) -> dict:
    """As the admin: a customer with a contact, an order for them, a catalog product."""
    customer = await client.post(
        "/api/v1/customers/",
        json={
            "name": "Acme",
            "contacts": [{"name": "Ann", "role": "buyer", "phone": "+380501112233", "email": "a@x.ua"}],
        },
    )
    assert customer.status_code in (200, 201), customer.text
    contact_id = customer.json()["contacts"][0]["id"]
    order = await client.post(
        "/api/v1/projects/", json={"name": "Mask order", "customer_id": customer.json()["id"], "contact_id": contact_id}
    )
    assert order.status_code in (200, 201), order.text
    product = await client.post("/api/v1/products/", json={"name": "Mask lamp"})
    assert product.status_code in (200, 201), product.text
    return {"customer": customer.json()["id"], "order": order.json()["id"], "product": product.json()["id"]}


@pytest.mark.asyncio
async def test_an_order_shows_its_contacts_phone_only_to_a_reader_of_customers(
    committing_client: AsyncClient, db_session
):
    desk = await _desk(committing_client)
    await _user(db_session, "mask_orders", ["orders:read"])
    await _user(db_session, "mask_orders_cust", ["orders:read", "customers:read"])

    plain = (await committing_client.get(f"/api/v1/projects/{desk['order']}", headers=await _jwt("mask_orders"))).json()
    assert plain["contact"]["name"] == "Ann"
    assert plain["contact"]["role"] == "buyer"
    assert plain["contact"]["phone"] is None
    assert plain["contact"]["email"] is None

    full = (
        await committing_client.get(f"/api/v1/projects/{desk['order']}", headers=await _jwt("mask_orders_cust"))
    ).json()
    assert full["contact"]["phone"] == "+380501112233"
    assert full["contact"]["email"] == "a@x.ua"


@pytest.mark.asyncio
async def test_the_sidebar_counts_only_what_the_reader_may_see(committing_client: AsyncClient, db_session):
    await _desk(committing_client)
    await _user(db_session, "mask_catalog", ["products:read"])
    await _user(db_session, "mask_nothing", ["inventory:read"])

    badges = await committing_client.get("/api/v1/projects/nav-badges", headers=await _jwt("mask_catalog"))
    assert badges.status_code == 200, badges.text
    body = badges.json()
    assert body["active_orders"] is None
    assert body["stock_below_min"] is None
    assert isinstance(body["draft_products"], int)

    refused = await committing_client.get("/api/v1/projects/nav-badges", headers=await _jwt("mask_nothing"))
    assert refused.status_code == 403


@pytest.mark.asyncio
async def test_a_product_hides_stock_and_order_figures_from_a_catalog_reader(
    committing_client: AsyncClient, db_session
):
    desk = await _desk(committing_client)
    await _user(db_session, "mask_cat_only", ["products:read"])
    await _user(db_session, "mask_cat_all", ["products:read", "stock:read", "orders:read"])

    plain = (
        await committing_client.get(f"/api/v1/products/{desk['product']}", headers=await _jwt("mask_cat_only"))
    ).json()
    for field in ("kits_available", "finished_available", "finished_positions", "finished_below_min"):
        assert plain[field] is None, field
    for field in ("lines_count", "active_orders_count", "orders_count", "units_printed_total"):
        assert plain[field] is None, field

    full = (
        await committing_client.get(f"/api/v1/products/{desk['product']}", headers=await _jwt("mask_cat_all"))
    ).json()
    assert isinstance(full["kits_available"], int)
    assert isinstance(full["orders_count"], int)

    rows = (await committing_client.get("/api/v1/products/", headers=await _jwt("mask_cat_only"))).json()
    assert rows and all(row["kits_available"] is None and row["active_orders_count"] is None for row in rows)


@pytest.mark.asyncio
async def test_a_filter_or_sort_on_a_hidden_field_is_refused(committing_client: AsyncClient, db_session):
    await _desk(committing_client)
    await _user(db_session, "mask_cat_filter", ["products:read"])
    headers = await _jwt("mask_cat_filter")

    by_stock = await committing_client.get(
        "/api/v1/products/", params={"page": 1, "stock": "finished"}, headers=headers
    )
    assert by_stock.status_code == 403
    assert by_stock.json()["detail"]["error"] == "workshop_read_required"
    by_orders = await committing_client.get(
        "/api/v1/products/", params={"page": 1, "sort_by": "orders-desc"}, headers=headers
    )
    assert by_orders.status_code == 403
    adhoc = await committing_client.get("/api/v1/products/", params={"page": 1, "include_adhoc": True}, headers=headers)
    assert adhoc.status_code == 403
    assert (await committing_client.get("/api/v1/products/", params={"page": 1}, headers=headers)).status_code == 200


@pytest.mark.asyncio
async def test_a_customer_hides_its_orders_money_from_a_reader_of_customers_only(
    committing_client: AsyncClient, db_session
):
    desk = await _desk(committing_client)
    await _user(db_session, "mask_cust_only", ["customers:read"])
    await _user(db_session, "mask_cust_orders", ["customers:read", "orders:read"])

    plain = (
        await committing_client.get(f"/api/v1/customers/{desk['customer']}", headers=await _jwt("mask_cust_only"))
    ).json()
    assert plain["figures"] is None
    assert plain["contacts"][0]["phone"] == "+380501112233"
    tiles = (await committing_client.get("/api/v1/customers/summary", headers=await _jwt("mask_cust_only"))).json()
    assert tiles["active_orders"] is None
    assert tiles["total_price"] is None

    full = (
        await committing_client.get(f"/api/v1/customers/{desk['customer']}", headers=await _jwt("mask_cust_orders"))
    ).json()
    assert full["figures"] is not None


@pytest.mark.asyncio
async def test_the_issue_dialogs_recipient_follows_the_shipping_or_contacts_right(
    committing_client: AsyncClient, db_session
):
    desk = await _desk(committing_client)
    await _user(db_session, "mask_ful_plain", ["orders:read"])
    await _user(db_session, "mask_ful_ship", ["orders:read", "stock:move"])

    plain = (
        await committing_client.get(
            f"/api/v1/projects/{desk['order']}/fulfilment", headers=await _jwt("mask_ful_plain")
        )
    ).json()
    assert plain["recipient"] is None
    shipped = (
        await committing_client.get(f"/api/v1/projects/{desk['order']}/fulfilment", headers=await _jwt("mask_ful_ship"))
    ).json()
    assert shipped["recipient"]["phone"] == "+380501112233"


@pytest.mark.asyncio
async def test_a_products_stock_names_the_order_a_movement_served_only_to_a_reader_of_orders(
    committing_client: AsyncClient, db_session
):
    product = (await committing_client.post("/api/v1/products/", json={"name": "Mask shelf"})).json()["id"]
    part = await committing_client.post(
        f"/api/v1/products/{product}/parts", json={"kind": "printed", "name": "shade", "qty_per_unit": 1}
    )
    assert part.status_code in (200, 201), part.text
    adjusted = await committing_client.post(
        f"/api/v1/products/{product}/stock/adjust", json={"part_id": part.json()["id"], "delta": 2, "note": "count"}
    )
    assert adjusted.status_code in (200, 201), adjusted.text
    order = await committing_client.post(
        "/api/v1/projects/",
        json={"name": "Shelf order", "lines": [{"product_id": product, "quantity": 1, "from_stock_units": 1}]},
    )
    assert order.status_code in (200, 201), order.text
    await _user(db_session, "mask_shelf", ["products:read", "stock:read"])
    await _user(db_session, "mask_shelf_orders", ["products:read", "stock:read", "orders:read"])

    def served(body):
        return [m for m in body["movements"] if m.get("project_line_id") is not None]

    [plain] = served(
        (await committing_client.get(f"/api/v1/products/{product}/stock", headers=await _jwt("mask_shelf"))).json()
    )
    assert plain["order_name"] is None
    assert plain["order_id"] is None
    [full] = served(
        (
            await committing_client.get(f"/api/v1/products/{product}/stock", headers=await _jwt("mask_shelf_orders"))
        ).json()
    )
    assert full["order_name"] == "Shelf order"


@pytest.mark.asyncio
@pytest.mark.parametrize("header", ["x-api-key", "bearer"])
async def test_a_mask_follows_an_api_keys_scope_as_well_as_its_owner(
    committing_client: AsyncClient, db_session, header
):
    """An API key reads within its scope AND its owner's rights (WS-13 E13 O12, final review):
    the owner keeps the contacts, but a key without the projects scope does not — the order it
    may read shows its contact without the phone."""
    from backend.tests.integration.test_workshop_library_rights import _key

    desk = await _desk(committing_client)
    owner = await _user(db_session, f"mask_key_{header}", ["orders:read", "customers:read"])
    status_only = await _key(db_session, owner, can_read_status=True, can_manage_projects=False)
    with_projects = await _key(db_session, owner, can_read_status=True, can_manage_projects=True)

    def headers(raw: str) -> dict:
        return {"X-API-Key": raw} if header == "x-api-key" else {"Authorization": f"Bearer {raw}"}

    url = f"/api/v1/projects/{desk['order']}"
    plain = await committing_client.get(url, headers=headers(status_only))
    assert plain.status_code == 200, plain.text
    assert plain.json()["contact"]["phone"] is None
    full = await committing_client.get(url, headers=headers(with_projects))
    assert full.json()["contact"]["phone"] == "+380501112233"
