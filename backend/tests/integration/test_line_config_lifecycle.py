"""A part, a line or an order that goes takes its configuration rows with it
(SQLite runs no FK actions — spec workshop-product-variants, rule 11)."""

import pytest
from sqlalchemy import select

from backend.app.models.line_config import ProjectLineChoice, ProjectLinePartCount
from backend.app.models.product import Product, ProductPart
from backend.app.models.product_variant import ProductVariantGroup, ProductVariantOption
from backend.app.models.project_line import ProjectLine
from backend.app.services import line_config
from backend.tests.fixtures.sliced_sources import slice_parts

pytestmark = pytest.mark.integration


@pytest.fixture
async def order(committing_client, db_session):
    product = Product(name="Pipe")
    db_session.add(product)
    await db_session.flush()
    group = ProductVariantGroup(product_id=product.id, name="Tail")
    db_session.add(group)
    await db_session.flush()
    opt = ProductVariantOption(group_id=group.id, name="straight")
    db_session.add(opt)
    await db_session.flush()
    group.default_option_id = opt.id
    flask = ProductPart(product_id=product.id, kind="printed", name="flask", name_key="flask", qty_per_unit=1)
    cap = ProductPart(product_id=product.id, kind="printed", name="cap", name_key="cap", qty_per_unit=1)
    db_session.add_all([flask, cap])
    await db_session.flush()
    # A variant product's line needs its kit sliced (spec product-part-variant-combinations, п. 10).
    await slice_parts(db_session, product.id, [flask, cap])
    await db_session.commit()
    body = (
        await committing_client.post(
            "/api/v1/projects/", json={"name": "O", "lines": [{"product_id": product.id, "quantity": 2}]}
        )
    ).json()
    line = await db_session.get(ProjectLine, body["lines"][0]["id"])
    await line_config.set_configuration(db_session, line, choices={}, counts={flask.id: 3}, actor=None)
    await db_session.commit()
    return {"project_id": body["id"], "line_id": line.id, "product": product, "flask": flask, "cap": cap}


async def _rows(db, model, **where):
    stmt = select(model)
    for key, value in where.items():
        stmt = stmt.where(getattr(model, key) == value)
    return (await db.execute(stmt)).scalars().all()


@pytest.mark.asyncio
async def test_a_deleted_part_takes_its_count_rows(committing_client, db_session, order):
    r = await committing_client.delete(f"/api/v1/products/{order['product'].id}/parts/{order['flask'].id}")
    assert r.status_code == 200, r.text
    assert await _rows(db_session, ProjectLinePartCount, part_id=order["flask"].id) == []
    assert (await committing_client.get(f"/api/v1/projects/{order['project_id']}")).status_code == 200


@pytest.mark.asyncio
async def test_a_merged_part_moves_its_count_rows_to_the_target(committing_client, db_session, order):
    r = await committing_client.post(
        f"/api/v1/products/{order['product'].id}/parts/{order['cap'].id}/merge",
        json={"source_part_id": order["flask"].id},
    )
    assert r.status_code == 200, r.text
    # The line wanted 3 flasks and its standard 1 cap; a merge says they are one
    # part, so it wants 4 of the target.
    rows = await _rows(db_session, ProjectLinePartCount, line_id=order["line_id"])
    assert [(row.part_id, row.qty) for row in rows] == [(order["cap"].id, 4)]


@pytest.mark.asyncio
async def test_a_deleted_line_takes_its_configuration(committing_client, db_session, order):
    r = await committing_client.delete(f"/api/v1/projects/{order['project_id']}/lines/{order['line_id']}")
    assert r.status_code == 200, r.text
    assert await _rows(db_session, ProjectLineChoice, line_id=order["line_id"]) == []
    assert await _rows(db_session, ProjectLinePartCount, line_id=order["line_id"]) == []


@pytest.mark.asyncio
async def test_a_deleted_order_takes_its_lines_configuration(committing_client, db_session, order):
    r = await committing_client.delete(f"/api/v1/projects/{order['project_id']}")
    assert r.status_code == 200, r.text
    assert await _rows(db_session, ProjectLineChoice, line_id=order["line_id"]) == []
    assert await _rows(db_session, ProjectLinePartCount, line_id=order["line_id"]) == []
