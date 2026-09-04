"""The stock ledger and its single writer (pass 8, Decisions 1, 4, 7).

Everything here goes through ``services/part_stock``, never through a row
inserted by hand — the point of the service is that it is the only writer, and
a test that reaches past it would be pinning a rule nothing enforces.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.dialects import postgresql, sqlite

from backend.app.models.part_stock import ProductPartStockMovement
from backend.app.models.product import Product, ProductPart
from backend.app.services.part_stock import (
    REASONS,
    PartStockError,
    balances,
    delete_for_part,
    kits_available,
    lock_part_stmt,
    move,
    movements,
    repoint,
)


async def _make_product(db_session, *parts: tuple[str, str, int]) -> tuple[Product, dict[str, ProductPart]]:
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


async def _row_count(db_session, part_id: int) -> int:
    return await db_session.scalar(
        select(func.count())
        .select_from(ProductPartStockMovement)
        .where(ProductPartStockMovement.product_part_id == part_id)
    )


@pytest.fixture
async def kit(db_session):
    """Five lids and three bases, one of each per unit — the spec's example."""
    product, parts = await _make_product(db_session, ("lid", "printed", 1), ("base", "printed", 1))
    await move(db_session, part_id=parts["lid"].id, delta=5, reason="unfiled_print")
    await move(db_session, part_id=parts["base"].id, delta=3, reason="unfiled_print")
    return product, parts


async def test_a_balance_is_the_sum_of_that_parts_movements(db_session):
    product, parts = await _make_product(db_session, ("lid", "printed", 1), ("base", "printed", 1))
    await move(db_session, part_id=parts["lid"].id, delta=5, reason="unfiled_print")
    await move(db_session, part_id=parts["lid"].id, delta=-2, reason="reserved_for_order")
    await move(db_session, part_id=parts["base"].id, delta=3, reason="surplus_banked")

    assert await balances(db_session, product.id) == {parts["lid"].id: 3, parts["base"].id: 3}


async def test_a_counted_part_with_no_movement_reads_zero_rather_than_missing(db_session):
    """The map is complete, so no caller has to invent the difference between
    "no stock" and "no key"."""
    product, parts = await _make_product(db_session, ("lid", "printed", 1), ("base", "printed", 1))
    await move(db_session, part_id=parts["lid"].id, delta=5, reason="unfiled_print")

    assert await balances(db_session, product.id) == {parts["lid"].id: 5, parts["base"].id: 0}


async def test_a_part_turned_purchased_loses_its_balance_but_keeps_its_history(db_session):
    """A bought screw is procurement, not stock — the same split
    ``order_metrics`` makes when it builds a line's figures. The movements the
    part made while it was still printed happened, though, so the history keeps
    them: ``balances`` filters, the ledger does not forget.
    """
    product, parts = await _make_product(db_session, ("lid", "printed", 1), ("bracket", "printed", 1))
    written = await move(db_session, part_id=parts["bracket"].id, delta=4, reason="unfiled_print")
    await move(db_session, part_id=parts["lid"].id, delta=5, reason="unfiled_print")

    parts["bracket"].kind = "purchased"
    await db_session.flush()

    assert await balances(db_session, product.id) == {parts["lid"].id: 5}
    assert written.id in {m.id for m in await movements(db_session, product.id)}


async def test_a_part_the_product_zeroes_loses_its_balance_but_keeps_its_history(db_session):
    """``qty_per_unit = 0`` is the product's own "do not measure me"."""
    product, parts = await _make_product(db_session, ("lid", "printed", 1), ("jig", "printed", 1))
    written = await move(db_session, part_id=parts["jig"].id, delta=9, reason="unfiled_print")

    parts["jig"].qty_per_unit = 0
    await db_session.flush()

    assert await balances(db_session, product.id) == {parts["lid"].id: 0}
    assert written.id in {m.id for m in await movements(db_session, product.id)}


async def test_kits_available_is_the_scarcest_counted_part(db_session, kit):
    """Five lids and three bases make three widgets; the two spare lids stay
    lids."""
    product, parts = kit
    assert kits_available(await balances(db_session, product.id), list(parts.values())) == 3


async def test_a_part_needed_twice_per_unit_halves_what_it_can_supply(db_session):
    product, parts = await _make_product(db_session, ("lid", "printed", 1), ("foot", "printed", 2))
    await move(db_session, part_id=parts["lid"].id, delta=5, reason="unfiled_print")
    await move(db_session, part_id=parts["foot"].id, delta=5, reason="unfiled_print")

    # 5 feet at 2 per unit is two kits and a spare foot, not two and a half.
    assert kits_available(await balances(db_session, product.id), list(parts.values())) == 2


async def test_a_product_with_no_counted_part_makes_no_kits(db_session):
    """Answering "unlimited" here would offer stock nobody has."""
    product, parts = await _make_product(db_session, ("screw", "purchased", 4))
    assert kits_available(await balances(db_session, product.id), list(parts.values())) == 0


async def test_move_refuses_a_reason_no_reader_knows(db_session):
    _p, parts = await _make_product(db_session, ("lid", "printed", 1))
    with pytest.raises(ValueError, match="unknown stock movement reason"):
        await move(db_session, part_id=parts["lid"].id, delta=1, reason="shrinkage")


async def test_move_refuses_a_part_that_does_not_exist(db_session):
    """SQLite would take the FK happily and the row would then be invisible to
    every reader, all of which join the part."""
    with pytest.raises(ValueError, match="no product part"):
        await move(db_session, part_id=987654, delta=1, reason="manual")


@pytest.mark.parametrize(("kind", "qty"), [("purchased", 4), ("printed", 0)])
async def test_moving_stock_on_a_part_that_has_none_is_a_caller_error(db_session, kind, qty):
    """Only a counted part has a balance, so a movement against a purchased
    part — or one the product zeroed — is a row nothing would ever read back.
    Refused at the door rather than written and lost."""
    _p, parts = await _make_product(db_session, ("thing", kind, qty))
    with pytest.raises(ValueError, match="not a counted printed part"):
        await move(db_session, part_id=parts["thing"].id, delta=1, reason="manual")


async def test_a_manual_correction_below_zero_is_refused(db_session):
    _p, parts = await _make_product(db_session, ("lid", "printed", 1))
    await move(db_session, part_id=parts["lid"].id, delta=2, reason="unfiled_print")

    with pytest.raises(PartStockError):
        await move(db_session, part_id=parts["lid"].id, delta=-3, reason="manual")


async def test_a_release_that_would_go_below_zero_is_refused_too(db_session):
    """``reservation_released`` carries a positive delta by construction, so a
    negative one big enough to go under is a bug in the caller, not a stale
    dialog — it fails loudly instead of being clamped into silence."""
    _p, parts = await _make_product(db_session, ("lid", "printed", 1))
    with pytest.raises(PartStockError):
        await move(db_session, part_id=parts["lid"].id, delta=-1, reason="reservation_released")


@pytest.mark.parametrize(
    ("reason", "delta"),
    [
        ("surplus_banked", -1),
        ("unfiled_print", -1),
        ("reservation_released", -1),
        ("reserved_for_order", 1),
    ],
)
async def test_a_reason_pointing_the_wrong_way_is_a_caller_error(db_session, reason, delta):
    """Banking, counting a print and releasing a reservation all put parts IN;
    a reservation takes them OUT. A delta pointing the other way is two
    operations confused, not stock that moved — and only ``manual`` may go
    either way, because only the operator knows which way a miscount went.

    Stock is stacked first so the wrong sign is refused for BEING wrong, not
    for taking the balance below zero.
    """
    _p, parts = await _make_product(db_session, ("lid", "printed", 1))
    await move(db_session, part_id=parts["lid"].id, delta=10, reason="unfiled_print")

    with pytest.raises(ValueError, match="moves stock"):
        await move(db_session, part_id=parts["lid"].id, delta=delta, reason=reason)


@pytest.mark.parametrize("reason", REASONS)
async def test_a_zero_delta_writes_no_row_whatever_the_reason(db_session, reason):
    """A ledger of no-ops is a ledger nobody reads to the end — and Decision 6
    puts this table in front of the operator."""
    _p, parts = await _make_product(db_session, ("lid", "printed", 1))
    await move(db_session, part_id=parts["lid"].id, delta=4, reason="unfiled_print")

    assert await move(db_session, part_id=parts["lid"].id, delta=0, reason=reason) is None
    assert await _row_count(db_session, parts["lid"].id) == 1


async def test_a_reservation_with_nothing_left_to_reserve_writes_no_row(db_session):
    """The clamp can land on zero — somebody else took the last kit while the
    dialog was open. Nothing moved, so nothing is recorded."""
    _p, parts = await _make_product(db_session, ("lid", "printed", 1))

    assert await move(db_session, part_id=parts["lid"].id, delta=-5, reason="reserved_for_order") is None
    assert await _row_count(db_session, parts["lid"].id) == 0


async def test_a_stale_reservation_takes_only_what_is_there(db_session):
    """Decision 4: the line dialog's default is computed when the dialog opens
    and pressed later. Between the two somebody else's line may have taken the
    stock — so the reservation shrinks to what is left and the movement says
    how much that was, rather than pushing the balance negative or refusing an
    order the operator is entitled to place."""
    product, parts = await _make_product(db_session, ("lid", "printed", 1))
    await move(db_session, part_id=parts["lid"].id, delta=3, reason="unfiled_print")

    written = await move(db_session, part_id=parts["lid"].id, delta=-5, reason="reserved_for_order")

    assert written is not None and written.delta == -3, "reserved more than the stock held"
    assert await balances(db_session, product.id) == {parts["lid"].id: 0}


def test_the_balance_read_locks_the_part_row_where_the_backend_has_a_lock():
    """Two operators reserving the last three kits at the same moment both read
    a balance of 3 and both take it. PostgreSQL emits ``FOR UPDATE`` and the
    second waits; SQLite's dialect emits nothing and does not need to — its
    writes are already serialised by one database-wide write lock. Both halves
    are asserted so the test says which backend does what.
    """
    stmt = lock_part_stmt(1)
    assert "FOR UPDATE" in str(stmt.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" not in str(stmt.compile(dialect=sqlite.dialect()))


async def test_movements_come_back_newest_first(db_session):
    product, parts = await _make_product(db_session, ("lid", "printed", 1))
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
    product, parts = await _make_product(db_session, ("lid", "printed", 1))
    written = [await move(db_session, part_id=parts["lid"].id, delta=1, reason="unfiled_print") for _ in range(5)]

    got = await movements(db_session, product.id, limit=2)

    assert [m.id for m in got] == [written[-1].id, written[-2].id]


async def test_movements_are_scoped_to_their_own_product(db_session):
    mine, my_parts = await _make_product(db_session, ("lid", "printed", 1))
    _theirs, their_parts = await _make_product(db_session, ("lid", "printed", 1))
    ours = await move(db_session, part_id=my_parts["lid"].id, delta=1, reason="unfiled_print")
    await move(db_session, part_id=their_parts["lid"].id, delta=1, reason="unfiled_print")

    assert [m.id for m in await movements(db_session, mine.id)] == [ours.id]


async def test_delete_for_part_takes_the_whole_ledger_of_that_part(db_session):
    """The FK cascade fires on PostgreSQL only, so the writer does it — and only
    for the part asked for."""
    product, parts = await _make_product(db_session, ("lid", "printed", 1), ("base", "printed", 1))
    await move(db_session, part_id=parts["lid"].id, delta=5, reason="unfiled_print")
    await move(db_session, part_id=parts["lid"].id, delta=2, reason="surplus_banked")
    kept = await move(db_session, part_id=parts["base"].id, delta=3, reason="unfiled_print")

    assert await delete_for_part(db_session, parts["lid"].id) == 2
    assert [m.id for m in await movements(db_session, product.id)] == [kept.id]


async def test_repoint_moves_the_stock_onto_the_part_it_was_merged_into(db_session):
    """Free stock is parts on a shelf; a merge says those parts are these parts,
    so the balance follows rather than evaporating."""
    product, parts = await _make_product(db_session, ("lid", "printed", 1), ("cover", "printed", 1))
    await move(db_session, part_id=parts["cover"].id, delta=4, reason="unfiled_print")
    await move(db_session, part_id=parts["lid"].id, delta=1, reason="unfiled_print")

    assert await repoint(db_session, from_part_id=parts["cover"].id, to_part_id=parts["lid"].id) == 1

    assert await balances(db_session, product.id) == {parts["lid"].id: 5, parts["cover"].id: 0}


async def test_repoint_refuses_a_target_that_cannot_hold_stock(db_session):
    _p, parts = await _make_product(db_session, ("lid", "printed", 1), ("screw", "purchased", 4))
    await move(db_session, part_id=parts["lid"].id, delta=4, reason="unfiled_print")

    with pytest.raises(ValueError, match="nowhere to go"):
        await repoint(db_session, from_part_id=parts["lid"].id, to_part_id=parts["screw"].id)


async def test_repoint_of_a_part_with_no_stock_is_allowed_anywhere(db_session):
    """Merging two purchased parts is an ordinary edit; refusing it because
    neither has a balance would break it for nothing."""
    _p, parts = await _make_product(db_session, ("screw", "purchased", 4), ("bolt", "purchased", 2))
    assert await repoint(db_session, from_part_id=parts["bolt"].id, to_part_id=parts["screw"].id) == 0
