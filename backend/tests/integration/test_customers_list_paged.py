"""GET /customers paged mode (spec projects-lists-parity, rules 1–7)."""

from datetime import datetime, timedelta

import pytest

from backend.app.models.customer import Customer
from backend.app.models.project import Project

pytestmark = pytest.mark.integration


async def _customer(db_session, name, contact=None, *, orders=0):
    c = Customer(name=name, contact=contact)
    db_session.add(c)
    await db_session.flush()
    for i in range(orders):
        db_session.add(Project(name=f"{name}-{i}", status="active", customer_id=c.id))
    await db_session.commit()
    await db_session.refresh(c)
    return c


@pytest.mark.asyncio
async def test_without_page_the_flat_shape_is_unchanged(async_client, db_session):
    await _customer(db_session, "Ivan")
    flat = await async_client.get("/api/v1/customers/")
    assert flat.status_code == 200
    assert isinstance(flat.json(), list)
    assert set(flat.json()[0]) >= {"id", "name", "contact", "figures"}


@pytest.mark.asyncio
async def test_page_gives_the_envelope(async_client, db_session):
    for name in ("a", "b", "c"):
        await _customer(db_session, name)
    body = (await async_client.get("/api/v1/customers/?page=2&per_page=2")).json()
    assert [c["name"] for c in body["items"]] == ["c"]
    assert body["meta"] == {"total": 3, "current_page": 2, "per_page": 2, "last_page": 2}
    assert "totals" not in body


@pytest.mark.asyncio
async def test_q_matches_name_or_contact(async_client, db_session):
    await _customer(db_session, "Ivan", contact="ivan@x.ua")
    await _customer(db_session, "Olena", contact="+380 50 000")
    by_contact = (await async_client.get("/api/v1/customers/?page=1&q=x.ua")).json()
    by_name = (await async_client.get("/api/v1/customers/?page=1&q=OLE")).json()
    assert [c["name"] for c in by_contact["items"]] == ["Ivan"]
    assert [c["name"] for c in by_name["items"]] == ["Olena"]


@pytest.mark.asyncio
async def test_a_computed_key_sorts_by_its_figure(async_client, db_session):
    for name, orders in (("a", 1), ("b", 3), ("c", 0), ("d", 2)):
        await _customer(db_session, name, orders=orders)
    body = (await async_client.get("/api/v1/customers/?page=1&per_page=2&sort_by=orders-desc")).json()
    assert [c["figures"]["projects"] for c in body["items"]] == [3, 2]
    assert body["meta"]["total"] == 4


@pytest.mark.asyncio
async def test_pages_never_overlap_or_skip_on_ties(async_client, db_session):
    for _ in range(7):
        await _customer(db_session, "same")
    seen = []
    for page in (1, 2, 3):
        body = (await async_client.get(f"/api/v1/customers/?page={page}&per_page=3&sort_by=name-asc")).json()
        seen += [c["id"] for c in body["items"]]
    assert len(seen) == 7 and len(set(seen)) == 7


@pytest.mark.asyncio
async def test_unknown_sort_is_the_default_not_a_400(async_client, db_session):
    await _customer(db_session, "b")
    await _customer(db_session, "a")
    r = await async_client.get("/api/v1/customers/?page=1&sort_by=bogus")
    assert r.status_code == 200
    assert [c["name"] for c in r.json()["items"]] == ["a", "b"]


# ---- final-review fix pass: pin the flat shape, prove every sort flips ----

T0 = datetime(2026, 1, 1, 12, 0, 0)


@pytest.mark.asyncio
async def test_the_flat_answer_is_pinned_and_ignores_the_paged_params(async_client, db_session):
    from backend.app.schemas.customer import CustomerResponse

    for name in ("b", "c", "a"):
        await _customer(db_session, name)
    flat = (await async_client.get("/api/v1/customers/")).json()
    assert [c["name"] for c in flat] == ["a", "b", "c"]
    assert set(flat[0]) == set(CustomerResponse.model_fields)
    same = (await async_client.get("/api/v1/customers/?q=zzz&sort_by=name-desc&per_page=1")).json()
    assert same == flat


def test_every_computed_key_has_its_figure():
    from backend.app.api.routes.customers import _CUSTOMER_COMPUTED, _CUSTOMER_SORT

    assert set(_CUSTOMER_COMPUTED) == _CUSTOMER_SORT.computed


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["name", "created", "orders", "active", "completed", "cancelled", "total_price"])
async def test_every_sort_key_really_orders_both_ways(async_client, db_session, key):
    """Distinct values for every key, so ascending must be descending reversed."""
    for i in range(4):
        c = Customer(name=f"c{i}", created_at=T0 + timedelta(days=i))
        db_session.add(c)
        await db_session.flush()
        # i+1 orders of each status, priced so the sums are distinct too.
        for status in ("active", "completed", "cancelled"):
            for j in range(i + 1):
                db_session.add(
                    Project(name=f"{c.name}-{status}-{j}", status=status, customer_id=c.id, price=10.0 * (i + 1))
                )
    await db_session.commit()
    asc = [c["id"] for c in (await async_client.get(f"/api/v1/customers/?page=1&sort_by={key}-asc")).json()["items"]]
    desc = [c["id"] for c in (await async_client.get(f"/api/v1/customers/?page=1&sort_by={key}-desc")).json()["items"]]
    assert len(asc) == 4
    assert asc == list(reversed(desc))


@pytest.mark.asyncio
async def test_names_sort_without_regard_to_case(async_client, db_session):
    for name in ("apple", "Zebra", "banana"):
        await _customer(db_session, name)
    items = (await async_client.get("/api/v1/customers/?page=1&sort_by=name-asc")).json()["items"]
    assert [c["name"] for c in items] == ["apple", "banana", "Zebra"]


@pytest.mark.asyncio
async def test_search_folds_cyrillic_case_on_the_contact(async_client, db_session):
    await _customer(db_session, "Olena", contact="Олена Коваль, склад")
    await _customer(db_session, "Ivan", contact="ivan@x.ua")
    items = (await async_client.get("/api/v1/customers/?page=1&q=КОВАЛЬ")).json()["items"]
    assert [c["name"] for c in items] == ["Olena"]
