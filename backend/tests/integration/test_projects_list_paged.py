"""GET /projects paged mode (spec projects-lists-parity, rules 1–7).

``page`` is the compat switch: without it the flat array every existing
consumer reads is untouched; with it the archive's envelope plus tab totals.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from backend.app.models.customer import Customer
from backend.app.models.project import Project

pytestmark = pytest.mark.integration


async def _customer(db_session, name, contact=None):
    c = Customer(name=name, contact=contact)
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    return c


async def _order(db_session, name, *, status="active", customer=None, priority="normal"):
    p = Project(name=name, status=status, priority=priority, customer_id=customer.id if customer else None)
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


@pytest.mark.asyncio
async def test_without_page_the_flat_shape_is_unchanged(async_client, db_session):
    await _order(db_session, "A")
    flat = await async_client.get("/api/v1/projects/")
    assert flat.status_code == 200
    assert isinstance(flat.json(), list)
    assert set(flat.json()[0]) >= {"id", "name", "status", "progress", "prints_queued"}


@pytest.mark.asyncio
async def test_page_gives_the_envelope_with_tab_totals_under_the_filters(async_client, db_session):
    acme = await _customer(db_session, "ACME")
    other = await _customer(db_session, "Other")
    await _order(db_session, "a1", customer=acme)
    await _order(db_session, "a2", customer=acme, status="completed")
    await _order(db_session, "o1", customer=other)

    r = await async_client.get(f"/api/v1/projects/?page=1&per_page=24&customer_id={acme.id}&status=active")
    body = r.json()
    assert r.status_code == 200
    assert [o["name"] for o in body["items"]] == ["a1"]
    assert body["meta"] == {"total": 1, "current_page": 1, "per_page": 24, "last_page": 1}
    # Tabs count under the customer filter, ignoring the status filter.
    assert body["totals"] == {"active": 1, "completed": 1, "cancelled": 0, "all": 2}


@pytest.mark.asyncio
async def test_q_matches_order_name_or_customer_name(async_client, db_session):
    acme = await _customer(db_session, "ACME Robotics")
    await _order(db_session, "Gearbox", customer=acme)
    await _order(db_session, "Lamp")
    by_customer = (await async_client.get("/api/v1/projects/?page=1&q=robot")).json()
    by_name = (await async_client.get("/api/v1/projects/?page=1&q=LAMP")).json()
    assert [o["name"] for o in by_customer["items"]] == ["Gearbox"]
    assert [o["name"] for o in by_name["items"]] == ["Lamp"]
    # The tabs follow the search too.
    assert by_customer["totals"]["all"] == 1


@pytest.mark.asyncio
async def test_priority_sorts_by_rank_not_alphabet(async_client, db_session):
    for name, priority in (("n", "normal"), ("u", "urgent"), ("l", "low"), ("h", "high")):
        await _order(db_session, name, priority=priority)
    body = (await async_client.get("/api/v1/projects/?page=1&sort_by=priority-desc")).json()
    assert [o["priority"] for o in body["items"]] == ["urgent", "high", "normal", "low"]


@pytest.mark.asyncio
async def test_a_computed_key_sorts_by_its_figure(async_client, db_session, monkeypatch):
    """``queued`` is not a column — the route computes every row's figures, sorts
    by the one asked for, then cuts the page."""
    from backend.app.api.routes import projects as route_module

    orders = [await _order(db_session, f"o{i}") for i in range(4)]
    queued = {orders[0].id: 2, orders[1].id: 9, orders[2].id: 0, orders[3].id: 5}

    async def figures(db, *, project_ids):
        return [
            SimpleNamespace(
                project_id=pid,
                ordered=0,
                printed=0,
                covered_units=0,
                remaining=0,
                from_stock_units=0,
                prints_in_progress=0,
                prints_queued=queued[pid],
                progress=0.0,
            )
            for pid in project_ids
        ]

    monkeypatch.setattr(route_module, "grouped_figures", figures)

    body = (await async_client.get("/api/v1/projects/?page=1&per_page=2&sort_by=queued-desc")).json()
    assert [o["prints_queued"] for o in body["items"]] == [9, 5]
    assert body["meta"]["total"] == 4 and body["meta"]["last_page"] == 2


@pytest.mark.asyncio
async def test_search_and_customer_sort_together(async_client, db_session):
    """Both need the customer join — it must be made once."""
    acme = await _customer(db_session, "ACME")
    await _order(db_session, "Gearbox", customer=acme)
    await _order(db_session, "Gear shelf")
    r = await async_client.get("/api/v1/projects/?page=1&q=gear&sort_by=customer-asc")
    assert r.status_code == 200, r.text
    assert [o["name"] for o in r.json()["items"]] == ["Gearbox", "Gear shelf"]  # NULL customer last


@pytest.mark.asyncio
async def test_pages_never_overlap_or_skip_on_ties(async_client, db_session):
    for _ in range(7):
        await _order(db_session, "same")  # identical name → id decides
    seen = []
    for page in (1, 2, 3):
        body = (await async_client.get(f"/api/v1/projects/?page={page}&per_page=3&sort_by=name-asc")).json()
        seen += [o["id"] for o in body["items"]]
    assert len(seen) == 7 and len(set(seen)) == 7
    assert body["meta"]["last_page"] == 3


@pytest.mark.asyncio
async def test_unknown_sort_is_the_default_not_a_400(async_client, db_session):
    await _order(db_session, "x")
    r = await async_client.get("/api/v1/projects/?page=1&sort_by=bogus")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_all_gives_everything_with_an_honest_meta(async_client, db_session):
    for i in range(5):
        await _order(db_session, f"o{i}")
    body = (await async_client.get("/api/v1/projects/?page=3&per_page=2&all=true")).json()
    assert len(body["items"]) == 5
    assert body["meta"] == {"total": 5, "current_page": 1, "per_page": 5, "last_page": 1}


# ---- final-review fix pass: pin the flat shape, prove every sort flips ----

T0 = datetime(2026, 1, 1, 12, 0, 0)


async def _dated_order(db_session, name, *, i, customer=None, priority="normal"):
    p = Project(
        name=name,
        status="active",
        priority=priority,
        customer_id=customer.id if customer else None,
        created_at=T0 + timedelta(days=i),
        updated_at=T0 + timedelta(days=10 - i),
        due_date=T0 + timedelta(days=20 + i),
    )
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


@pytest.mark.asyncio
async def test_the_flat_answer_is_pinned_and_ignores_the_paged_params(async_client, db_session):
    """Rule 1 byte for byte: the order (updated_at desc), the key set, and that
    `q` / `sort_by` mean nothing without `page` — twenty flat readers rely on it."""
    from backend.app.schemas.project import ProjectListResponse

    rows = [await _dated_order(db_session, n, i=i) for i, n in enumerate(("b-first", "a-second", "c-third"))]
    flat = (await async_client.get("/api/v1/projects/")).json()
    assert [o["id"] for o in flat] == [r.id for r in rows]  # updated_at desc: i=0 is the newest
    assert set(flat[0]) == set(ProjectListResponse.model_fields)
    same = (await async_client.get("/api/v1/projects/?q=zzz&sort_by=name-asc&per_page=1")).json()
    assert same == flat


def test_every_computed_key_has_its_figure():
    from backend.app.api.routes.projects import _ORDER_COMPUTED, _ORDER_SORT

    assert set(_ORDER_COMPUTED) == _ORDER_SORT.computed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key", ["updated", "created", "name", "due", "priority", "customer", "progress", "remaining", "printing", "queued"]
)
async def test_every_sort_key_really_orders_both_ways(async_client, db_session, monkeypatch, key):
    """Distinct values for every key, so ascending must be descending reversed —
    a key that silently fell back to the default would answer the same list twice."""
    from backend.app.api.routes import projects as route_module

    customers = [await _customer(db_session, f"cust-{c}") for c in "dbac"]
    orders = [
        await _dated_order(db_session, f"o{i}", i=i, customer=customers[i], priority=p)
        for i, p in enumerate(("low", "urgent", "normal", "high"))
    ]
    figure = {o.id: i for i, o in enumerate(orders)}

    async def figures(db, *, project_ids):
        return [
            SimpleNamespace(
                project_id=pid,
                ordered=10,
                printed=0,
                covered_units=0,
                remaining=figure[pid] * 3,
                from_stock_units=0,
                prints_in_progress=figure[pid] + 1,
                prints_queued=10 - figure[pid],
                progress=figure[pid] / 10,
            )
            for pid in project_ids
        ]

    monkeypatch.setattr(route_module, "grouped_figures", figures)
    asc = [o["id"] for o in (await async_client.get(f"/api/v1/projects/?page=1&sort_by={key}-asc")).json()["items"]]
    desc = [o["id"] for o in (await async_client.get(f"/api/v1/projects/?page=1&sort_by={key}-desc")).json()["items"]]
    assert len(asc) == 4
    assert asc == list(reversed(desc))


@pytest.mark.asyncio
async def test_names_sort_without_regard_to_case(async_client, db_session):
    acme = await _customer(db_session, "acme")
    zulu = await _customer(db_session, "Zulu")
    for name, customer in (("apple", zulu), ("Zebra", acme), ("banana", None)):
        await _order(db_session, name, customer=customer)
    by_name = (await async_client.get("/api/v1/projects/?page=1&sort_by=name-asc")).json()["items"]
    assert [o["name"] for o in by_name] == ["apple", "banana", "Zebra"]
    by_customer = (await async_client.get("/api/v1/projects/?page=1&sort_by=customer-asc")).json()["items"]
    assert [o["customer_name"] for o in by_customer] == ["acme", "Zulu", None]


@pytest.mark.asyncio
async def test_search_folds_cyrillic_case(async_client, db_session):
    shop = await _customer(db_session, "Ламповий цех")
    await _order(db_session, "Абажур", customer=shop)
    await _order(db_session, "лампа настільна")
    by_customer = (await async_client.get("/api/v1/projects/?page=1&q=ЛАМПОВИЙ")).json()["items"]
    by_name = (await async_client.get("/api/v1/projects/?page=1&q=ЛАМПА")).json()["items"]
    assert [o["name"] for o in by_customer] == ["Абажур"]
    assert [o["name"] for o in by_name] == ["лампа настільна"]
