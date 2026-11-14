"""A product's lines in the orders list it is filtered by (WS-13 E9 A02).

The product page's «Orders» tab says, per order, how many of THIS product the order
holds and in which configuration. ``GET /projects/?product_id=`` therefore answers each
row's lines of that product — ``{line_id, mode, quantity, configuration}`` in line order
— read in one batched pass for the rows of the page, never per row. Without
``product_id`` nothing is read and the field is ``null``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import event

from backend.app.models.product import Product, ProductPart
from backend.app.models.product_variant import ProductVariantGroup, ProductVariantOption
from backend.tests.fixtures.part_bindings import bind
from backend.tests.fixtures.sliced_sources import slice_parts

pytestmark = pytest.mark.integration


@pytest.fixture
async def pipe(db_session):
    """«Pipe»: flask always, straight tail (standard) or angled tail."""
    product = Product(name="Pipe")
    other = Product(name="Other")
    db_session.add_all([product, other])
    await db_session.flush()
    group = ProductVariantGroup(product_id=product.id, name="Tail")
    db_session.add(group)
    await db_session.flush()
    straight = ProductVariantOption(group_id=group.id, name="straight", position=0)
    angled = ProductVariantOption(group_id=group.id, name="angled", position=1)
    db_session.add_all([straight, angled])
    await db_session.flush()
    group.default_option_id = straight.id
    flask = ProductPart(product_id=product.id, kind="printed", name="flask", name_key="flask", qty_per_unit=1)
    s_tail = ProductPart(
        product_id=product.id,
        kind="printed",
        name="straight tail",
        name_key="straight tail",
        qty_per_unit=1,
        sort_order=1,
    )
    a_tail = ProductPart(
        product_id=product.id,
        kind="printed",
        name="angled tail",
        name_key="angled tail",
        qty_per_unit=1,
        sort_order=2,
    )
    x = ProductPart(product_id=other.id, kind="printed", name="x", name_key="x", qty_per_unit=1)
    bind(s_tail, straight)
    bind(a_tail, angled)
    db_session.add_all([flask, s_tail, a_tail, x])
    await db_session.flush()
    # A variant product's line needs its kit sliced (spec product-part-variant-combinations, п. 10).
    await slice_parts(db_session, product.id, [flask, s_tail, a_tail])
    await db_session.commit()
    return {"product": product.id, "other": other.id, "group": group.id, "angled": angled.id, "flask": flask.id}


async def _order(client, name: str, lines: list[dict]) -> dict:
    r = await client.post("/api/v1/projects/", json={"name": name, "lines": lines})
    assert r.status_code == 200, r.text
    return r.json()


async def _rows(client, **params) -> dict[int, dict]:
    r = await client.get("/api/v1/projects/", params=params)
    assert r.status_code == 200, r.text
    body = r.json()
    items = body["items"] if isinstance(body, dict) else body
    return {row["id"]: row for row in items}


@pytest.mark.asyncio
async def test_each_order_names_its_lines_of_the_product_in_line_order(committing_client, pipe):
    mixed = await _order(
        committing_client,
        "Mixed",
        [
            {"product_id": pipe["product"], "quantity": 2, "choices": {str(pipe["group"]): pipe["angled"]}},
            {"product_id": pipe["other"], "quantity": 5},
            {"product_id": pipe["product"], "quantity": 3},
            {"product_id": pipe["product"], "mode": "parts", "part_counts": {str(pipe["flask"]): 4}},
        ],
    )
    rows = await _rows(committing_client, product_id=pipe["product"], page=1)
    lines = rows[mixed["id"]]["product_lines"]
    own = [line["id"] for line in mixed["lines"] if line["product_id"] == pipe["product"]]
    assert [line["line_id"] for line in lines] == own
    assert [(line["mode"], line["quantity"]) for line in lines] == [("product", 2), ("product", 3), ("parts", 1)]
    angled, standard, parts = (line["configuration"] for line in lines)
    assert [(c["option_name"], c["is_default"]) for c in angled["choices"]] == [("angled", False)]
    assert [(c["option_name"], c["is_default"]) for c in standard["choices"]] == [("straight", True)]
    assert parts["choices"] == [] and [(p["name"], p["qty"]) for p in parts["changed_parts"]] == [("flask", 4)]


@pytest.mark.asyncio
async def test_without_product_id_nothing_is_read(committing_client, pipe):
    order = await _order(committing_client, "One", [{"product_id": pipe["product"], "quantity": 1}])
    paged = await _rows(committing_client, page=1)
    assert paged[order["id"]]["product_lines"] is None
    flat = await _rows(committing_client)
    assert flat[order["id"]]["product_lines"] is None


@pytest.mark.asyncio
async def test_the_flat_list_by_product_carries_them_too(committing_client, pipe):
    order = await _order(committing_client, "One", [{"product_id": pipe["product"], "quantity": 4}])
    flat = await _rows(committing_client, product_id=pipe["product"])
    assert [line["quantity"] for line in flat[order["id"]]["product_lines"]] == [4]


@pytest.mark.asyncio
async def test_the_statements_do_not_grow_with_the_page(committing_client, db_session, pipe):
    selects: list[str] = []

    def count(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    async def measure() -> int:
        selects.clear()
        event.listen(db_session.bind.sync_engine, "before_cursor_execute", count)
        try:
            await _rows(committing_client, product_id=pipe["product"], page=1)
        finally:
            event.remove(db_session.bind.sync_engine, "before_cursor_execute", count)
        return len(selects)

    # The first order has the same shape as the later ones (a product line and a parts
    # line), and a warm-up read goes first: only the number of orders may change.
    await _order(
        committing_client,
        "First",
        [
            {"product_id": pipe["product"], "quantity": 1, "choices": {str(pipe["group"]): pipe["angled"]}},
            {"product_id": pipe["product"], "mode": "parts", "part_counts": {str(pipe["flask"]): 2}},
        ],
    )
    await measure()
    one = await measure()
    for n in range(3):
        await _order(
            committing_client,
            f"More {n}",
            [
                {"product_id": pipe["product"], "quantity": n + 1},
                {"product_id": pipe["product"], "mode": "parts", "part_counts": {str(pipe["flask"]): 2}},
            ],
        )
    assert await measure() == one
