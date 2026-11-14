"""Assembly: parts of one configuration leave the free-parts shelf, the position
grows — one transaction for both ledgers (spec workshop-finished-goods, rules 5, 10, 12–13)."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from backend.app.models.finished_stock import StockItemMovement
from backend.app.models.part_stock import ProductPartStockMovement
from backend.app.models.product import Product, ProductPart
from backend.app.models.product_variant import ProductVariantGroup, ProductVariantOption
from backend.app.models.user import User
from backend.app.services import finished_stock, part_stock
from backend.tests.fixtures.part_bindings import bind

pytestmark = pytest.mark.integration


@pytest.fixture
async def pipe(db_session):
    """flask ×1; tail straight ×1 (standard) or angled ×1 with 2 purchased screws; a zeroed clip."""
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
        ),
        "angled": ProductPart(
            product_id=product.id,
            kind="printed",
            name="angled",
            name_key="angled",
            qty_per_unit=1,
        ),
        "screw": ProductPart(
            product_id=product.id,
            kind="purchased",
            name="screw",
            name_key="purchased:screw",
            qty_per_unit=2,
        ),
        "clip": ProductPart(product_id=product.id, kind="printed", name="clip", name_key="clip", qty_per_unit=0),
    }
    bind(parts["flask"])
    bind(parts["straight"], straight)
    bind(parts["angled"], angled)
    bind(parts["screw"], angled)
    bind(parts["clip"])
    db_session.add_all(parts.values())
    await db_session.flush()
    for name, n in (("flask", 5), ("straight", 5), ("angled", 1)):
        await part_stock.move(db_session, part_id=parts[name].id, delta=n, reason="manual", note="seed")
    await db_session.commit()
    return {"product": product, "group": group, "straight": straight, "angled": angled, "parts": parts}


async def _rows(db):
    finished = (await db.execute(select(func.count(StockItemMovement.id)))).scalar()
    parts = (await db.execute(select(func.count(ProductPartStockMovement.id)))).scalar()
    return finished, parts


@pytest.mark.asyncio
async def test_assembling_moves_both_ledgers(db_session, pipe):
    person = User(username="builder", password_hash="x", role="user")
    db_session.add(person)
    await db_session.flush()
    item = await finished_stock.item_for(db_session, pipe["product"].id, {}, create=True)
    move = await finished_stock.assemble(db_session, item, 3, note="batch", actor=person)
    assert (move.kind, move.delta_on_hand, item.on_hand) == ("assembled", 3, 3)
    balances = await part_stock.balances(db_session, pipe["product"].id)
    assert balances[pipe["parts"]["flask"].id] == 2 and balances[pipe["parts"]["straight"].id] == 2
    assert balances[pipe["parts"]["angled"].id] == 1
    rows = (
        (
            await db_session.execute(
                select(ProductPartStockMovement).where(ProductPartStockMovement.reason == "assembled")
            )
        )
        .scalars()
        .all()
    )
    assert sorted((r.product_part_id, r.delta) for r in rows) == sorted(
        [(pipe["parts"]["flask"].id, -3), (pipe["parts"]["straight"].id, -3)]
    )
    assert {(r.stock_item_id, r.note, r.created_by) for r in rows} == {(item.id, "assembled", person.id)}


@pytest.mark.asyncio
async def test_assembly_beyond_the_shelf_writes_nothing(db_session, pipe):
    item = await finished_stock.item_for(
        db_session, pipe["product"].id, {pipe["group"].id: pipe["angled"].id}, create=True
    )
    before = await _rows(db_session)
    with pytest.raises(finished_stock.FinishedStockError) as e:
        await finished_stock.assemble(db_session, item, 2)
    assert str(e.value) == "Only 1 can be assembled from the free parts"
    assert await _rows(db_session) == before
    assert item.on_hand == 0


@pytest.mark.asyncio
async def test_purchased_parts_are_not_written_off(db_session, pipe):
    item = await finished_stock.item_for(
        db_session, pipe["product"].id, {pipe["group"].id: pipe["angled"].id}, create=True
    )
    await finished_stock.assemble(db_session, item, 1)
    touched = (
        (
            await db_session.execute(
                select(ProductPartStockMovement.product_part_id).where(ProductPartStockMovement.reason == "assembled")
            )
        )
        .scalars()
        .all()
    )
    assert pipe["parts"]["screw"].id not in touched


@pytest.mark.asyncio
async def test_a_configuration_with_a_part_without_a_shelf_cannot_be_assembled(db_session, pipe):
    clip = pipe["parts"]["clip"]
    item = await finished_stock.item_for(db_session, pipe["product"].id, {}, {clip.id: 1}, create=True)
    assert await finished_stock.can_assemble_item(db_session, item) == 0
    with pytest.raises(finished_stock.FinishedStockError):
        await finished_stock.assemble(db_session, item, 1)


@pytest.mark.asyncio
async def test_part_movements_are_stamped_in_utc(db_session, pipe):
    # The clock is Python's UTC, never the server default (local time on a
    # PostgreSQL whose zone is not UTC) — so the two ledgers merge in order.
    assert ProductPartStockMovement.__table__.c.created_at.default is not None
    move = await part_stock.move(db_session, part_id=pipe["parts"]["flask"].id, delta=1, reason="manual", note="x")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs(move.created_at - now) < timedelta(seconds=5)


@pytest.mark.asyncio
async def test_deleting_a_user_detaches_both_ledgers(committing_client, db_session, pipe):
    person = User(username="leaver", password_hash="x", role="user")
    db_session.add(person)
    await db_session.flush()
    item = await finished_stock.item_for(db_session, pipe["product"].id, {}, create=True)
    await finished_stock.receive(db_session, item, 1, actor=person)
    await part_stock.move(
        db_session, part_id=pipe["parts"]["flask"].id, delta=1, reason="manual", note="x", created_by=person.id
    )
    await db_session.commit()
    r = await committing_client.delete(f"/api/v1/users/{person.id}")
    assert r.status_code == 204, r.text
    db_session.expire_all()
    assert (
        await db_session.execute(select(StockItemMovement.created_by).where(StockItemMovement.created_by.is_not(None)))
    ).all() == []
    assert (
        await db_session.execute(
            select(ProductPartStockMovement.created_by).where(ProductPartStockMovement.created_by.is_not(None))
        )
    ).all() == []


@pytest.mark.asyncio
async def test_a_configuration_of_purchased_parts_only_cannot_be_assembled(db_session):
    """Nothing printed, nothing on a shelf: «can assemble» is 0 and the request writes
    nothing in either ledger (Review Focus 5)."""
    kit = Product(name="Kit")
    db_session.add(kit)
    await db_session.flush()
    db_session.add(
        ProductPart(product_id=kit.id, kind="purchased", name="bolt", name_key="purchased:bolt", qty_per_unit=4)
    )
    await db_session.commit()
    item = await finished_stock.item_for(db_session, kit.id, {}, create=True)
    before = await _rows(db_session)
    assert await finished_stock.can_assemble_item(db_session, item) == 0
    with pytest.raises(finished_stock.FinishedStockError):
        await finished_stock.assemble(db_session, item, 1)
    assert await _rows(db_session) == before
