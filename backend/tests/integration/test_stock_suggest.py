"""What an order line would take from stock — the server's proposal (spec workshop-add-to-order, rules 5, 10)."""

from contextlib import contextmanager

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

from backend.app.models.product import Product, ProductPart
from backend.app.models.product_variant import ProductVariantGroup, ProductVariantOption
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.app.services import finished_stock, line_config, part_stock
from backend.tests.fixtures.part_bindings import bind
from backend.tests.fixtures.sliced_sources import slice_parts

pytestmark = pytest.mark.integration


@pytest.fixture
async def farm(db_session):
    """Pipe (Tail: straight standard / angled), shelf flask 4 · straight 3 · angled 1,
    standard position with 2 ready units; Lamp (shade 2 on the shelf, no position)."""
    pipe = Product(name="Pipe")
    lamp = Product(name="Lamp")
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
        ),
        "angled": ProductPart(
            product_id=pipe.id,
            kind="printed",
            name="angled",
            name_key="angled",
            qty_per_unit=1,
        ),
        "shade": ProductPart(product_id=lamp.id, kind="printed", name="shade", name_key="shade", qty_per_unit=1),
    }
    bind(parts["flask"])
    bind(parts["straight"], straight)
    bind(parts["angled"], angled)
    bind(parts["shade"])
    db_session.add_all(parts.values())
    await db_session.flush()
    # A variant product's line needs its kit sliced (spec product-part-variant-combinations, п. 10).
    await slice_parts(db_session, pipe.id, [parts["flask"], parts["straight"], parts["angled"]])
    for name, n in (("flask", 4), ("straight", 3), ("angled", 1), ("shade", 2)):
        await part_stock.move(db_session, part_id=parts[name].id, delta=n, reason="manual", note="seed")
    standard = await finished_stock.item_for(db_session, pipe.id, {}, create=True)
    await finished_stock.receive(db_session, standard, 2)
    await db_session.commit()
    return {"pipe": pipe, "lamp": lamp, "straight": straight, "angled": angled, "standard": standard}


@pytest.fixture
async def line_holding_two(db_session, farm):
    """An active order line on the standard Pipe holding both ready units."""
    order = Project(name="O")
    db_session.add(order)
    await db_session.flush()
    line = ProjectLine(project_id=order.id, product_id=farm["pipe"].id, quantity=2)
    db_session.add(line)
    await db_session.flush()
    await line_config.seed_line(db_session, line, choices=None, counts=None)
    assert await finished_stock.reserve_for_line(db_session, line, 2) == 2
    await db_session.commit()
    return line


async def _suggest(client, items):
    r = await client.post("/api/v1/stock/suggest", json={"items": items})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_ready_units_first_then_kits(committing_client, farm):
    s = (await _suggest(committing_client, [{"product_id": farm["pipe"].id, "quantity": 6}]))["items"][0]
    assert (s["finished_free"], s["kits_free"]) == (2, 3)
    assert (s["from_finished"], s["from_kits"], s["to_print"]) == (2, 3, 1)
    assert s["position_code"] == f"SK-{farm['standard'].id:04d}"


@pytest.mark.asyncio
async def test_another_configuration_does_not_count(committing_client, farm):
    s = (
        await _suggest(
            committing_client, [{"product_id": farm["pipe"].id, "options": [farm["angled"].id], "quantity": 3}]
        )
    )["items"][0]
    assert (s["finished_free"], s["kits_free"], s["from_finished"], s["from_kits"]) == (0, 1, 0, 1)
    assert s["position_code"] is None


@pytest.mark.asyncio
async def test_a_small_quantity_takes_only_what_it_needs(committing_client, farm):
    s = (await _suggest(committing_client, [{"product_id": farm["pipe"].id, "quantity": 1}]))["items"][0]
    assert (s["from_finished"], s["from_kits"], s["to_print"]) == (1, 0, 0)


@pytest.mark.asyncio
async def test_the_answer_keeps_the_order_of_the_question(committing_client, farm):
    body = await _suggest(
        committing_client,
        [{"product_id": farm["lamp"].id, "quantity": 5}, {"product_id": farm["pipe"].id, "quantity": 2}],
    )
    assert [s["product_id"] for s in body["items"]] == [farm["lamp"].id, farm["pipe"].id]
    lamp = body["items"][0]
    assert (lamp["finished_free"], lamp["kits_free"], lamp["from_kits"], lamp["to_print"]) == (0, 2, 2, 3)


@pytest.mark.asyncio
async def test_an_existing_lines_own_reservation_counts_as_free(committing_client, farm, line_holding_two):
    s = (await _suggest(committing_client, [{"product_id": farm["pipe"].id, "quantity": 2}]))["items"][0]
    assert s["finished_free"] == 0  # the line holds both
    s = (
        await _suggest(
            committing_client, [{"product_id": farm["pipe"].id, "quantity": 2, "line_id": line_holding_two.id}]
        )
    )["items"][0]
    assert s["finished_free"] == 2 and s["from_finished"] == 2  # …but it may keep its own


@pytest.mark.asyncio
async def test_many_items_read_the_shelf_once(committing_client, farm, monkeypatch):
    calls = []
    original = part_stock.balances_for_products

    async def counting(db, ids):
        calls.append(tuple(ids))
        return await original(db, ids)

    monkeypatch.setattr(part_stock, "balances_for_products", counting)
    items = [{"product_id": farm["pipe"].id, "quantity": q} for q in (1, 2, 3)]
    await _suggest(committing_client, [*items, {"product_id": farm["lamp"].id, "quantity": 1}])
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_refusals(committing_client, farm):
    r = await committing_client.post("/api/v1/stock/suggest", json={"items": [{"product_id": 999999, "quantity": 1}]})
    assert r.status_code == 404 and r.json()["detail"] == "Product not found"
    body = {
        "items": [{"product_id": farm["pipe"].id, "options": [farm["straight"].id, farm["angled"].id], "quantity": 1}]
    }
    r = await committing_client.post("/api/v1/stock/suggest", json=body)
    assert r.status_code == 422 and r.json()["detail"] == "Pick one option per group"
    body = {"items": [{"product_id": farm["lamp"].id, "options": [farm["angled"].id], "quantity": 1}]}
    r = await committing_client.post("/api/v1/stock/suggest", json=body)
    assert r.status_code == 422 and r.json()["detail"] == "That option does not belong to this product"
    assert (await committing_client.post("/api/v1/stock/suggest", json={"items": []})).status_code == 422
    body = {"items": [{"product_id": farm["pipe"].id, "quantity": 0}]}
    assert (await committing_client.post("/api/v1/stock/suggest", json=body)).status_code == 422


@contextmanager
def _statements():
    seen: list[str] = []

    def _record(_conn, _cursor, statement, _parameters, _context, _executemany):
        seen.append(statement)

    event.listen(Engine, "before_cursor_execute", _record)
    try:
        yield seen
    finally:
        event.remove(Engine, "before_cursor_execute", _record)


@pytest.mark.asyncio
async def test_existing_lines_cost_a_fixed_number_of_statements(committing_client, db_session, farm):
    """Final review M1: naming lines (line_id) does not add statements per line."""
    order = Project(name="O")
    db_session.add(order)
    await db_session.flush()
    lines = []
    for _ in range(3):
        line = ProjectLine(project_id=order.id, product_id=farm["pipe"].id, quantity=1)
        db_session.add(line)
        await db_session.flush()
        await line_config.seed_line(db_session, line, choices=None, counts=None)
        lines.append(line)
    await db_session.commit()

    async def cost(ids):
        items = [{"product_id": farm["pipe"].id, "quantity": 1, "line_id": i} for i in ids]
        with _statements() as seen:
            await _suggest(committing_client, items)
        return len(seen)

    await cost([lines[0].id])  # warm-up: the first request of a client fills caches of its own
    assert await cost([lines[0].id]) == await cost([line.id for line in lines])


@pytest.mark.asyncio
async def test_an_unknown_product_is_404_even_with_options(committing_client, farm):
    """Final review M2: the product is looked up before its options are judged."""
    body = {"items": [{"product_id": 999999, "options": [farm["straight"].id], "quantity": 1}]}
    r = await committing_client.post("/api/v1/stock/suggest", json=body)
    assert r.status_code == 404 and r.json()["detail"] == "Product not found"
