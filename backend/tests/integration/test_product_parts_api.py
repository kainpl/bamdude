"""The add-to-order dialog's server lists (spec workshop-add-to-order, rules 15–16): the
catalog row's ready units and facets, and the printed parts of catalogue products by page."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select, update

from backend.app.models.library import LibraryFile
from backend.app.models.product import Product, ProductOrigin, ProductPart
from backend.app.models.product_variant import ProductVariantGroup, ProductVariantOption
from backend.app.services import finished_stock
from backend.tests.fixtures.part_bindings import bind
from backend.tests.integration.test_product_catalog_list import seeded_catalog  # noqa: F401 — shared fixture

pytestmark = pytest.mark.integration


@pytest.fixture
async def pipe(db_session):
    """Pipe: flask always, a straight tail (standard) or an angled one, a bought screw."""
    product = Product(name="Pipe", sku="PP-1")
    db_session.add(product)
    await db_session.flush()
    group = ProductVariantGroup(product_id=product.id, name="Tail")
    db_session.add(group)
    await db_session.flush()
    straight = ProductVariantOption(group_id=group.id, name="straight", position=0)
    angled = ProductVariantOption(group_id=group.id, name="angled", position=1)
    db_session.add_all([straight, angled])
    await db_session.flush()
    group.default_option_id = straight.id
    db_session.add_all(
        [
            ProductPart(product_id=product.id, kind="printed", name="flask", name_key="flask", qty_per_unit=1),
            bind(
                ProductPart(
                    product_id=product.id,
                    kind="printed",
                    name="straight tail",
                    name_key="straight tail",
                    qty_per_unit=1,
                ),
                straight,
            ),
            bind(
                ProductPart(
                    product_id=product.id,
                    kind="printed",
                    name="angled tail",
                    name_key="angled tail",
                    qty_per_unit=1,
                ),
                angled,
            ),
            ProductPart(
                product_id=product.id, kind="purchased", name="screw", name_key="purchased:screw", qty_per_unit=2
            ),
        ]
    )
    job = Product(name="One-off", origin=ProductOrigin.ADHOC_PLATE.value)
    db_session.add(job)
    await db_session.flush()
    db_session.add(
        ProductPart(product_id=job.id, kind="printed", name="tail of a job", name_key="tail of a job", qty_per_unit=1)
    )
    await db_session.commit()
    return product


async def _parts(client, **params):
    r = await client.get("/api/v1/products/parts", params={"page": 1, **params})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_the_catalog_row_carries_ready_units_and_facets(committing_client, db_session, seeded_catalog):
    item = await finished_stock.item_for(db_session, seeded_catalog["lamp"], {}, create=True)
    await finished_stock.receive(db_session, item, 3)
    await finished_stock.reserve(db_session, item, 1)
    await db_session.commit()
    rows = {r["name"]: r for r in (await committing_client.get("/api/v1/products", params={"page": 1})).json()["items"]}
    assert rows["Lamp"]["finished_available"] == 2 and rows["Hook"]["finished_available"] == 0
    assert rows["Lamp"]["materials"] == ["PETG"] and rows["Lamp"]["models"] == ["P1S"]
    assert rows["Lamp"]["colors"]  # the plate's colour, normalised as the facets store it
    assert rows["Hook"]["materials"] == ["PLA"] and rows["Hook"]["models"] == ["X1C"]


@pytest.mark.asyncio
async def test_a_trashed_files_facets_are_not_the_products(committing_client, db_session, seeded_catalog):
    await db_session.execute(
        update(LibraryFile).where(LibraryFile.id == seeded_catalog["lamp_file"]).values(deleted_at=datetime.now(UTC))
    )
    await db_session.commit()
    rows = {r["name"]: r for r in (await committing_client.get("/api/v1/products", params={"page": 1})).json()["items"]}
    assert rows["Lamp"]["materials"] == [] and rows["Lamp"]["models"] == []


@pytest.mark.asyncio
async def test_a_page_reads_facets_and_positions_once(committing_client, test_engine, seeded_catalog):
    statements = []

    def spy(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", spy)
    try:
        assert (await committing_client.get("/api/v1/products", params={"page": 1})).status_code == 200
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", spy)
    assert sum("FROM product_facets" in s for s in statements) == 1
    assert sum("FROM stock_items" in s for s in statements) == 1


@pytest.mark.asyncio
async def test_parts_are_searched_by_their_name_the_product_and_its_sku(committing_client, pipe, seeded_catalog):
    body = await _parts(committing_client, q="tail")
    assert sorted(r["name"] for r in body["items"]) == ["angled tail", "straight tail"]
    angled = next(r for r in body["items"] if r["name"] == "angled tail")
    assert angled["variants"] == [{"group": "Tail", "options": ["angled"]}]
    assert "variant" not in angled
    assert angled["product"] == {"id": pipe.id, "code": f"PR-{pipe.id:04d}", "name": "Pipe", "sku": "PP-1"}
    assert [r["name"] for r in (await _parts(committing_client, q="pp-1 flask"))["items"]] == ["flask"]
    shade = (await _parts(committing_client, q="shade"))["items"]
    assert [r["name"] for r in shade] == ["shade.stl"] or [r["name"] for r in shade] == ["shade"]
    assert shade[0]["variants"] == [] and shade[0]["models"] == ["P1S"]


@pytest.mark.asyncio
async def test_a_part_bound_in_two_groups_names_each_group_once(committing_client, db_session, pipe):
    """The picker's label lists every group the part is bound in, in the groups' order,
    and each group's values in their order (spec product-part-variant-combinations)."""
    wall = ProductVariantGroup(product_id=pipe.id, name="Wall", position=1)
    db_session.add(wall)
    await db_session.flush()
    thin = ProductVariantOption(group_id=wall.id, name="thin", position=0)
    thick = ProductVariantOption(group_id=wall.id, name="thick", position=1)
    db_session.add_all([thin, thick])
    await db_session.flush()
    wall.default_option_id = thin.id
    tail = (
        await db_session.execute(
            select(ProductPart).where(ProductPart.product_id == pipe.id, ProductPart.name == "angled tail")
        )
    ).scalar_one()
    angled = (
        await db_session.execute(select(ProductVariantOption).where(ProductVariantOption.name == "angled"))
    ).scalar_one()
    bind(tail, angled, thick, thin)
    await db_session.commit()
    row = next(r for r in (await _parts(committing_client, q="angled"))["items"] if r["name"] == "angled tail")
    assert row["variants"] == [
        {"group": "Tail", "options": ["angled"]},
        {"group": "Wall", "options": ["thin", "thick"]},
    ]


@pytest.mark.asyncio
async def test_bought_parts_and_one_off_products_are_never_listed(committing_client, pipe):
    names = [r["name"] for r in (await _parts(committing_client, per_page=200))["items"]]
    assert "screw" not in names and "tail of a job" not in names


@pytest.mark.asyncio
async def test_the_model_filter_reads_the_products_facets(committing_client, pipe, seeded_catalog):
    names = [r["name"] for r in (await _parts(committing_client, model="p1s"))["items"]]
    assert names and all("shade" in n for n in names)


@pytest.mark.asyncio
async def test_parts_page_and_sort_on_the_server(committing_client, pipe, seeded_catalog):
    first = await _parts(committing_client, per_page=2, sort_by="product-asc")
    assert first["meta"]["total"] >= 4 and len(first["items"]) == 2
    desc = await _parts(committing_client, per_page=200, sort_by="product-desc")
    products = [r["product"]["name"] for r in desc["items"]]
    assert products == sorted(products, key=str.casefold, reverse=True)
