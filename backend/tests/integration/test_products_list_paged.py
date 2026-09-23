"""GET /products paged mode (spec projects-lists-parity, rules 1–7)."""

from datetime import datetime, timedelta

import pytest

from backend.app.models.product import Product, ProductPart

pytestmark = pytest.mark.integration


async def _product(db_session, name, *, active=True, origin="catalog", parts=0):
    p = Product(name=name, is_active=active, origin=origin)
    db_session.add(p)
    await db_session.flush()
    for i in range(parts):
        db_session.add(ProductPart(product_id=p.id, name=f"{name}-part{i}", name_key=f"{name}-part{i}", qty_per_unit=1))
    await db_session.commit()
    await db_session.refresh(p)
    return p


@pytest.mark.asyncio
async def test_without_page_the_flat_shape_is_unchanged(async_client, db_session):
    await _product(db_session, "Flask")
    flat = await async_client.get("/api/v1/products/")
    assert flat.status_code == 200
    assert isinstance(flat.json(), list)
    assert set(flat.json()[0]) >= {"id", "name", "parts_count", "plates_count", "lines_count", "kits_available"}


@pytest.mark.asyncio
async def test_page_gives_the_envelope_under_the_existing_filters(async_client, db_session):
    await _product(db_session, "Active one")
    await _product(db_session, "Retired", active=False)
    await _product(db_session, "Adhoc", origin="adhoc_job")
    body = (await async_client.get("/api/v1/products/?page=1&per_page=24&active=true")).json()
    assert [p["name"] for p in body["items"]] == ["Active one"]
    assert body["meta"] == {"total": 1, "current_page": 1, "per_page": 24, "last_page": 1}
    assert "totals" not in body


@pytest.mark.asyncio
async def test_q_searches_the_name(async_client, db_session):
    await _product(db_session, "Desk lamp")
    await _product(db_session, "Gear")
    body = (await async_client.get("/api/v1/products/?page=1&q=LAMP")).json()
    assert [p["name"] for p in body["items"]] == ["Desk lamp"]


@pytest.mark.asyncio
async def test_a_computed_key_sorts_by_its_figure(async_client, db_session):
    for name, parts in (("a", 1), ("b", 3), ("c", 0), ("d", 2)):
        await _product(db_session, name, parts=parts)
    body = (await async_client.get("/api/v1/products/?page=1&per_page=2&sort_by=parts-desc")).json()
    assert [p["parts_count"] for p in body["items"]] == [3, 2]
    assert body["meta"]["total"] == 4 and body["meta"]["last_page"] == 2


@pytest.mark.asyncio
async def test_pages_never_overlap_or_skip_on_ties(async_client, db_session):
    for _ in range(7):
        await _product(db_session, "same")
    seen = []
    for page in (1, 2, 3):
        body = (await async_client.get(f"/api/v1/products/?page={page}&per_page=3&sort_by=name-asc")).json()
        seen += [p["id"] for p in body["items"]]
    assert len(seen) == 7 and len(set(seen)) == 7
    assert body["meta"]["last_page"] == 3


@pytest.mark.asyncio
async def test_unknown_sort_is_the_default_not_a_400(async_client, db_session):
    await _product(db_session, "b")
    await _product(db_session, "a")
    r = await async_client.get("/api/v1/products/?page=1&sort_by=bogus")
    assert r.status_code == 200
    assert [p["name"] for p in r.json()["items"]] == ["a", "b"]


# ---- final-review fix pass: pin the flat shape, prove every sort flips ----

T0 = datetime(2026, 1, 1, 12, 0, 0)


@pytest.mark.asyncio
async def test_the_flat_answer_is_pinned_and_ignores_the_paged_params(async_client, db_session):
    from backend.app.schemas.product import ProductListItem

    for name in ("b", "c", "a"):
        await _product(db_session, name)
    flat = (await async_client.get("/api/v1/products/")).json()
    assert [p["name"] for p in flat] == ["a", "b", "c"]
    assert set(flat[0]) == set(ProductListItem.model_fields)
    same = (await async_client.get("/api/v1/products/?sort_by=name-desc&per_page=1")).json()
    assert same == flat


def test_every_computed_key_has_its_figure():
    from backend.app.api.routes.products import _PRODUCT_COMPUTED, _PRODUCT_SORT

    assert set(_PRODUCT_COMPUTED) == _PRODUCT_SORT.computed


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["name", "updated", "created", "parts", "plates", "orders", "kits"])
async def test_every_sort_key_really_orders_both_ways(async_client, db_session, monkeypatch, key):
    """Distinct values for every key, so ascending must be descending reversed."""
    from backend.app.api.routes import products as route_module
    from backend.app.models.project import Project
    from backend.app.models.project_line import ProjectLine
    from backend.app.services import part_stock

    products = []
    for i in range(4):
        p = await _product(db_session, f"p{i}", parts=i)
        p.created_at = T0 + timedelta(days=i)
        p.updated_at = T0 + timedelta(days=10 - i)
        products.append(p)
    order = Project(name="o", status="active")
    db_session.add(order)
    await db_session.flush()
    for i, p in enumerate(products):
        for _ in range(3 - i):  # lines per product: 3, 2, 1, 0
            db_session.add(ProjectLine(project_id=order.id, product_id=p.id, quantity=1))
    await db_session.commit()
    rank = {p.id: i for i, p in enumerate(products)}

    async def plates(db, product_ids):
        return {pid: rank[pid] * 2 for pid in product_ids}

    monkeypatch.setattr(route_module, "_plates_count", plates)
    monkeypatch.setattr(part_stock, "kits_available", lambda balances, parts: 7 - len(parts))

    asc = [p["id"] for p in (await async_client.get(f"/api/v1/products/?page=1&sort_by={key}-asc")).json()["items"]]
    desc = [p["id"] for p in (await async_client.get(f"/api/v1/products/?page=1&sort_by={key}-desc")).json()["items"]]
    assert len(asc) == 4
    assert asc == list(reversed(desc))


@pytest.mark.asyncio
async def test_names_sort_without_regard_to_case(async_client, db_session):
    for name in ("apple", "Zebra", "banana"):
        await _product(db_session, name)
    items = (await async_client.get("/api/v1/products/?page=1&sort_by=name-asc")).json()["items"]
    assert [p["name"] for p in items] == ["apple", "banana", "Zebra"]
