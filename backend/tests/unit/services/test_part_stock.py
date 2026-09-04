"""The stock ledger and its single writer (pass 8, Decisions 1, 4, 7).

Everything here goes through ``services/part_stock``, never through a row
inserted by hand — the point of the service is that it is the only writer, and
a test that reaches past it would be pinning a rule nothing enforces.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import update

from backend.app.models.part_stock import ProductPartStockMovement
from backend.app.models.product import Product, ProductPart
from backend.app.services.part_stock import (
    PartStockError,
    balances,
    kits_available,
    move,
    movements,
)


async def _product(db_session, *parts: tuple[str, str, int]) -> tuple[Product, dict[str, ProductPart]]:
    """A product and its parts, keyed by name: ``("lid", "printed", 1)``."""
    product = Product(name="Widget")
    db_session.add(product)
    await db_session.flush()
    made: dict[str, ProductPart] = {}
    for sort_order, (name, kind, qty) in enumerate(parts):
        part = ProductPart(
            product_id=product.id,
            kind=kind,
            name=name,
            name_key=name if kind == "printed" else f"purchased:{name}",
            qty_per_unit=qty,
            sort_order=sort_order,
        )
        db_session.add(part)
        made[name] = part
    await db_session.flush()
    return product, made


@pytest.fixture
async def kit(db_session):
    """Five lids and three bases, one of each per unit — the spec's example."""
    product, parts = await _product(db_session, ("lid", "printed", 1), ("base", "printed", 1))
    await move(db_session, part_id=parts["lid"].id, delta=5, reason="unfiled_print")
    await move(db_session, part_id=parts["base"].id, delta=3, reason="unfiled_print")
    return product, parts


async def test_a_balance_is_the_sum_of_that_parts_movements(db_session):
    product, parts = await _product(db_session, ("lid", "printed", 1), ("base", "printed", 1))
    await move(db_session, part_id=parts["lid"].id, delta=5, reason="unfiled_print")
    await move(db_session, part_id=parts["lid"].id, delta=-2, reason="reserved_for_order")
    await move(db_session, part_id=parts["base"].id, delta=3, reason="surplus_banked")

    assert await balances(db_session, product.id) == {parts["lid"].id: 3, parts["base"].id: 3}


async def test_a_counted_part_with_no_movement_reads_zero_rather_than_missing(db_session):
    """The map is complete, so no caller has to invent the difference between
    "no stock" and "no key"."""
    product, parts = await _product(db_session, ("lid", "printed", 1), ("base", "printed", 1))
    await move(db_session, part_id=parts["lid"].id, delta=5, reason="unfiled_print")

    assert await balances(db_session, product.id) == {parts["lid"].id: 5, parts["base"].id: 0}


async def test_a_purchased_part_has_no_balance_even_when_it_has_movements(db_session):
    """A bought screw is procurement, not stock — the same split
    ``order_metrics`` makes when it builds a line's figures."""
    product, parts = await _product(db_session, ("lid", "printed", 1), ("screw", "purchased", 4))
    await move(db_session, part_id=parts["lid"].id, delta=5, reason="unfiled_print")
    await move(db_session, part_id=parts["screw"].id, delta=100, reason="manual")

    assert await balances(db_session, product.id) == {parts["lid"].id: 5}


async def test_a_part_the_product_zeroed_has_no_balance(db_session):
    """``qty_per_unit = 0`` is the product's own "do not measure me"."""
    product, parts = await _product(db_session, ("lid", "printed", 1), ("jig", "printed", 0))
    await move(db_session, part_id=parts["jig"].id, delta=9, reason="unfiled_print")

    assert await balances(db_session, product.id) == {parts["lid"].id: 0}


async def test_kits_available_is_the_scarcest_counted_part(db_session, kit):
    """Five lids and three bases make three widgets; the two spare lids stay
    lids."""
    product, parts = kit
    assert kits_available(await balances(db_session, product.id), list(parts.values())) == 3


async def test_a_part_needed_twice_per_unit_halves_what_it_can_supply(db_session):
    product, parts = await _product(db_session, ("lid", "printed", 1), ("foot", "printed", 2))
    await move(db_session, part_id=parts["lid"].id, delta=5, reason="unfiled_print")
    await move(db_session, part_id=parts["foot"].id, delta=5, reason="unfiled_print")

    # 5 feet at 2 per unit is two kits and a spare foot, not two and a half.
    assert kits_available(await balances(db_session, product.id), list(parts.values())) == 2


async def test_a_product_with_no_counted_part_makes_no_kits(db_session):
    """Answering "unlimited" here would offer stock nobody has."""
    product, parts = await _product(db_session, ("screw", "purchased", 4))
    assert kits_available(await balances(db_session, product.id), list(parts.values())) == 0


async def test_move_refuses_a_reason_no_reader_knows(db_session):
    _product_, parts = await _product(db_session, ("lid", "printed", 1))
    with pytest.raises(ValueError, match="unknown stock movement reason"):
        await move(db_session, part_id=parts["lid"].id, delta=1, reason="shrinkage")


async def test_move_refuses_a_part_that_does_not_exist(db_session):
    """SQLite would take the FK happily and the row would then be invisible to
    every reader, all of which join the part."""
    with pytest.raises(ValueError, match="no product part"):
        await move(db_session, part_id=987654, delta=1, reason="manual")


async def test_a_manual_correction_below_zero_is_refused(db_session):
    _product_, parts = await _product(db_session, ("lid", "printed", 1))
    await move(db_session, part_id=parts["lid"].id, delta=2, reason="unfiled_print")

    with pytest.raises(PartStockError):
        await move(db_session, part_id=parts["lid"].id, delta=-3, reason="manual")


async def test_a_release_that_would_go_below_zero_is_refused_too(db_session):
    """``reservation_released`` carries a positive delta by construction, so a
    negative one big enough to go under is a bug in the caller, not a stale
    dialog — it fails loudly instead of being clamped into silence."""
    _product_, parts = await _product(db_session, ("lid", "printed", 1))
    with pytest.raises(PartStockError):
        await move(db_session, part_id=parts["lid"].id, delta=-1, reason="reservation_released")


async def test_a_stale_reservation_takes_only_what_is_there(db_session):
    """Decision 4: the line dialog's default is computed when the dialog opens
    and pressed later. Between the two somebody else's line may have taken the
    stock — so the reservation shrinks to what is left and the movement says
    how much that was, rather than pushing the balance negative or refusing an
    order the operator is entitled to place."""
    _product_, parts = await _product(db_session, ("lid", "printed", 1))
    await move(db_session, part_id=parts["lid"].id, delta=3, reason="unfiled_print")

    written = await move(db_session, part_id=parts["lid"].id, delta=-5, reason="reserved_for_order")

    assert written.delta == -3, "reserved more than the stock held"
    assert await balances(db_session, _product_.id) == {parts["lid"].id: 0}


async def test_movements_come_back_newest_first(db_session):
    product, parts = await _product(db_session, ("lid", "printed", 1))
    first = await move(db_session, part_id=parts["lid"].id, delta=5, reason="unfiled_print")
    second = await move(db_session, part_id=parts["lid"].id, delta=1, reason="manual", note="found one")
    third = await move(db_session, part_id=parts["lid"].id, delta=2, reason="surplus_banked")
    # Stamped out of insertion order so the assertion pins created_at, not the
    # id tie-break that carries same-second rows.
    base = datetime(2026, 9, 4, 12, 0, 0)
    for movement, offset in ((first, 0), (second, 2), (third, 1)):
        await db_session.execute(
            update(ProductPartStockMovement)
            .where(ProductPartStockMovement.id == movement.id)
            .values(created_at=base + timedelta(hours=offset))
        )

    assert [m.id for m in await movements(db_session, product.id)] == [second.id, third.id, first.id]


async def test_movements_stops_at_the_limit(db_session):
    product, parts = await _product(db_session, ("lid", "printed", 1))
    written = [await move(db_session, part_id=parts["lid"].id, delta=1, reason="unfiled_print") for _ in range(5)]

    got = await movements(db_session, product.id, limit=2)

    assert [m.id for m in got] == [written[-1].id, written[-2].id]


async def test_movements_are_scoped_to_their_own_product(db_session):
    mine, my_parts = await _product(db_session, ("lid", "printed", 1))
    _theirs, their_parts = await _product(db_session, ("lid", "printed", 1))
    ours = await move(db_session, part_id=my_parts["lid"].id, delta=1, reason="unfiled_print")
    await move(db_session, part_id=their_parts["lid"].id, delta=1, reason="unfiled_print")

    assert [m.id for m in await movements(db_session, mine.id)] == [ours.id]


async def test_a_movement_of_a_purchased_part_still_shows_in_the_history(db_session):
    """``balances`` drops it; the history does not. It happened, and a history
    that hides what happened is not one."""
    product, parts = await _product(db_session, ("screw", "purchased", 4))
    written = await move(db_session, part_id=parts["screw"].id, delta=10, reason="manual", note="miscount")

    assert [m.id for m in await movements(db_session, product.id)] == [written.id]
