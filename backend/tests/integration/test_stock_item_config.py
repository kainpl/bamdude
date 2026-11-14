"""A stock position's configuration is kept by the one configuration writer,
with the same rules as an order line's (spec workshop-finished-goods, rule 14)."""

import pytest
from sqlalchemy import select

from backend.app.models.finished_stock import StockItem, StockItemChoice, StockItemPartCount
from backend.app.models.product import Product, ProductPart
from backend.app.models.product_variant import ProductVariantGroup, ProductVariantOption
from backend.app.services import line_config
from backend.app.services.line_composition import compositions_for_items, load_item_configs
from backend.tests.fixtures.part_bindings import bind

pytestmark = pytest.mark.integration


@pytest.fixture
async def pipe(db_session):
    """flask ×1 always; tail straight ×1 (standard) or angled ×1."""
    product = Product(name="Pipe")
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
    parts = {
        "flask": ProductPart(product_id=product.id, kind="printed", name="flask", name_key="flask", qty_per_unit=1),
        "straight": ProductPart(
            product_id=product.id,
            kind="printed",
            name="straight",
            name_key="straight",
            qty_per_unit=1,
            sort_order=1,
        ),
        "angled": ProductPart(
            product_id=product.id,
            kind="printed",
            name="angled",
            name_key="angled",
            qty_per_unit=1,
            sort_order=2,
        ),
    }
    bind(parts["flask"])
    bind(parts["straight"], straight)
    bind(parts["angled"], angled)
    db_session.add_all(parts.values())
    await db_session.commit()
    return {"product": product, "group": group, "straight": straight, "angled": angled, "parts": parts}


async def _item(db, pipe, choices=None, counts=None):
    key, choices, counts = await line_config.resolve(db, pipe["product"].id, choices or {}, counts or {})
    item = StockItem(product_id=pipe["product"].id, config_key=key)
    db.add(item)
    await db.flush()
    await line_config.seed_item(db, item, choices, counts)
    await db.commit()
    return item


async def _key(db, item_id):
    return await db.scalar(select(StockItem.config_key).where(StockItem.id == item_id))


@pytest.mark.asyncio
async def test_a_position_records_the_standard_choice(db_session, pipe):
    item = await _item(db_session, pipe)
    assert item.config_key == f"{pipe['group'].id}={pipe['straight'].id}"
    rows = (await db_session.execute(select(StockItemChoice.option_id))).scalars().all()
    assert rows == [pipe["straight"].id]


@pytest.mark.asyncio
async def test_a_new_group_adds_its_standard_to_every_position(committing_client, db_session, pipe):
    item = await _item(db_session, pipe)
    r = await committing_client.post(
        f"/api/v1/products/{pipe['product'].id}/variant-groups", json={"name": "Mount", "options": ["desk", "wall"]}
    )
    assert r.status_code == 200, r.text
    mount = next(g for g in r.json()["variant_groups"] if g["name"] == "Mount")
    configs = await load_item_configs(db_session, [item.id])
    assert configs[item.id].choices[mount["id"]] == mount["default_option_id"]
    assert str(mount["default_option_id"]) in await _key(db_session, item.id)


@pytest.mark.asyncio
async def test_binding_a_part_freezes_positions(committing_client, db_session, pipe):
    item = await _item(db_session, pipe)
    flask = pipe["parts"]["flask"]
    r = await committing_client.patch(
        f"/api/v1/products/{pipe['product'].id}/parts/{flask.id}", json={"variant_option_ids": [pipe["angled"].id]}
    )
    assert r.status_code == 200, r.text
    rows = (
        await db_session.execute(
            select(StockItemPartCount.part_id, StockItemPartCount.qty).where(StockItemPartCount.item_id == item.id)
        )
    ).all()
    assert rows == [(flask.id, 1)]
    assert (await _key(db_session, item.id)).endswith(f"|{flask.id}=1")


@pytest.mark.asyncio
async def test_deleting_a_part_rekeys_positions(committing_client, db_session, pipe):
    flask = pipe["parts"]["flask"]
    item = await _item(db_session, pipe, counts={flask.id: 2})
    assert "|" in item.config_key
    r = await committing_client.delete(f"/api/v1/products/{pipe['product'].id}/parts/{flask.id}")
    assert r.status_code == 200, r.text
    assert await _key(db_session, item.id) == f"{pipe['group'].id}={pipe['straight'].id}"


@pytest.mark.asyncio
async def test_a_part_change_that_would_merge_two_positions_is_refused(committing_client, db_session, pipe):
    flask = pipe["parts"]["flask"]
    standard = await _item(db_session, pipe)
    more = await _item(db_session, pipe, counts={flask.id: 2})
    before = (standard.config_key, more.config_key)
    r = await committing_client.delete(f"/api/v1/products/{pipe['product'].id}/parts/{flask.id}")
    assert r.status_code == 409
    assert r.json()["detail"] == "That change would make two stock positions the same configuration"
    assert (await _key(db_session, standard.id), await _key(db_session, more.id)) == before
    assert await db_session.get(ProductPart, flask.id) is not None


@pytest.mark.asyncio
async def test_positions_read_their_composition_through_the_one_reader(db_session, pipe):
    item = await _item(db_session, pipe, choices={pipe["group"].id: pipe["angled"].id})
    parts = list(pipe["parts"].values())
    comps = await compositions_for_items(db_session, [item], {pipe["product"].id: parts})
    assert sorted(p.name for p, _per in comps[item.id]) == ["angled", "flask"]


@pytest.mark.asyncio
async def test_forgetting_positions_removes_their_configuration(db_session, pipe):
    item = await _item(db_session, pipe, counts={pipe["parts"]["flask"].id: 3})
    await line_config.forget_items(db_session, [item.id])
    await db_session.commit()
    assert (await db_session.execute(select(StockItemChoice))).scalars().all() == []
    assert (await db_session.execute(select(StockItemPartCount))).scalars().all() == []


@pytest.mark.asyncio
async def test_a_part_merge_that_would_merge_two_positions_is_refused(committing_client, db_session, pipe):
    """Merging «straight» into «flask»: a position that asked for two straight tails wants
    1 + 2 = 3 flasks, and so does the one that asked for three flasks and no tail — the same
    kit, two shelves. Refused, nothing merged (Review Focus 4, review Minor 14). A position
    that changed neither part follows the product, as an order line does."""
    flask, straight = pipe["parts"]["flask"], pipe["parts"]["straight"]
    standard = await _item(db_session, pipe, counts={straight.id: 2})
    doubled = await _item(db_session, pipe, counts={flask.id: 3, straight.id: 0})
    before = (standard.config_key, doubled.config_key)
    r = await committing_client.post(
        f"/api/v1/products/{pipe['product'].id}/parts/{flask.id}/merge", json={"source_part_id": straight.id}
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "That change would make two stock positions the same configuration"
    assert (await _key(db_session, standard.id), await _key(db_session, doubled.id)) == before
    assert await db_session.get(ProductPart, straight.id) is not None
