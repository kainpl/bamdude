"""The stock ledger and its single writer (pass 8, Decisions 1, 4, 7).

Everything here goes through ``services/part_stock``, never through a row
inserted by hand — the point of the service is that it is the only writer, and
a test that reaches past it would be pinning a rule nothing enforces.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.dialects import postgresql, sqlite

from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart
from backend.app.models.part_stock import ProductPartStockMovement
from backend.app.models.product import Product, ProductPart, ProductPlate
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.app.services import part_stock as part_stock_module
from backend.app.services.part_stock import (
    NOTE_FILED_UNDER_ORDER,
    NOTE_LINE_DELETED,
    NOTE_ORDER_CANCELLED,
    NOTE_RESERVATION_REWRITTEN,
    NOTE_TOKENS,
    REASONS,
    PartStockError,
    balances,
    credit_unfiled_print,
    delete_for_part,
    delete_for_parts,
    detach_archive,
    detach_line,
    kits_available,
    lock_part_stmt,
    move,
    movements,
    release_for_line,
    repoint,
    reserve_for_line,
    reserved_units_by_line,
    reserved_units_for_line,
    reverse_unfiled_print,
    unfiled_credit_net,
)
from backend.tests.unit.services.test_product_composition import counting_statements


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


# ---------- Decision 3: a print that belongs to no order ----------


async def _archive(db_session, *, file_id: int, plate_index: int = 1, status: str = "completed", project_id=None):
    archive = PrintArchive(
        project_id=project_id,
        library_file_id=file_id,
        plate_index=plate_index,
        filename="plate.gcode.3mf",
        file_path="",
        file_size=0,
        status=status,
    )
    db_session.add(archive)
    await db_session.flush()
    return archive


async def _rows(db_session, archive, *rows: tuple[str, int, int]) -> None:
    """``("lid", printed, defective)`` part rows for an archive."""
    db_session.add_all(
        [
            PrintArchivePart(archive_id=archive.id, name=key, name_key=key, quantity=quantity, defective=defective)
            for key, quantity, defective in rows
        ]
    )
    await db_session.flush()


@pytest.fixture
async def shelf(db_session):
    """A product whose single-plate file yields lids and bases, and one finished
    print of it that nobody filed under an order."""
    product, parts = await _make_product(db_session, ("lid", "printed", 1), ("base", "printed", 1))
    # ``plate_index = 0`` on the product plate is "the whole file", which is what
    # a single-plate 3MF gets from the sync; the PRINT carries the slicer's own
    # index, 1. Production produces exactly this mismatch.
    db_session.add(ProductPlate(product_id=product.id, library_file_id=77, plate_index=0))
    archive = await _archive(db_session, file_id=77)
    await _rows(db_session, archive, ("lid", 4, 1), ("base", 4, 0))
    return product, parts, archive


async def test_a_finished_print_with_no_order_lands_on_the_shelf(db_session, shelf):
    """Decision 3: one movement per part, ``printed - defective`` — the same
    arithmetic ``order_metrics.row_quantity`` does for a line."""
    product, parts, archive = shelf

    written = await credit_unfiled_print(db_session, archive)

    assert {m.reason for m in written} == {"unfiled_print"}
    assert {m.archive_id for m in written} == {archive.id}
    assert await balances(db_session, product.id) == {parts["lid"].id: 3, parts["base"].id: 4}


async def test_a_second_completion_event_for_the_same_print_credits_nothing(db_session, shelf):
    """An MQTT replay or a reconnect flap re-runs the completion handler. The
    check is the archive's NET (`unfiled_credit_net`), which is still positive
    — not "a row exists", which would also refuse the legitimate re-credit of a
    print somebody un-filed."""
    product, parts, archive = shelf
    await credit_unfiled_print(db_session, archive)

    assert await credit_unfiled_print(db_session, archive) == []
    assert await balances(db_session, product.id) == {parts["lid"].id: 3, parts["base"].id: 4}


async def test_a_print_filed_under_an_order_is_not_free_stock(db_session, shelf):
    """The order's own figures count those parts; crediting them here too would
    count every part of every order twice."""
    product, parts, archive = shelf
    archive.project_id = 5
    await db_session.flush()

    assert await credit_unfiled_print(db_session, archive) == []
    assert await balances(db_session, product.id) == {parts["lid"].id: 0, parts["base"].id: 0}


@pytest.mark.parametrize("status", ["printing", "failed", "cancelled"])
async def test_only_a_finished_print_reaches_the_shelf(db_session, shelf, status):
    """A running print's parts are not on a shelf yet and a failed one's never
    will be — ``attribute`` makes the same split with ``_DONE``."""
    product, parts, archive = shelf
    archive.status = status
    await db_session.flush()

    assert await credit_unfiled_print(db_session, archive) == []
    assert await balances(db_session, product.id) == {parts["lid"].id: 0, parts["base"].id: 0}


async def test_an_object_no_product_counts_is_skipped_not_refused(db_session):
    """A raft, a test cube, a part the product zeroed: attribution ignores it
    and so does stock. Silence, because it is a statement the product made."""
    product, parts = await _make_product(db_session, ("lid", "printed", 1), ("jig", "printed", 0))
    db_session.add(ProductPlate(product_id=product.id, library_file_id=77, plate_index=0))
    archive = await _archive(db_session, file_id=77)
    await _rows(db_session, archive, ("lid", 2, 0), ("jig", 9, 0), ("calibration_cube", 3, 0))

    written = await credit_unfiled_print(db_session, archive)

    assert [m.product_part_id for m in written] == [parts["lid"].id]
    assert await balances(db_session, product.id) == {parts["lid"].id: 2}


async def test_a_plate_no_product_claims_credits_nothing(db_session):
    """The ordinary case for most of a farm's prints — and the reason this
    returns an empty list rather than raising at the completion handler."""
    archive = await _archive(db_session, file_id=404)
    await _rows(db_session, archive, ("lid", 2, 0))

    assert await credit_unfiled_print(db_session, archive) == []


async def test_a_shared_file_credits_the_product_that_owns_each_object(db_session):
    """Two products on one bed: the lid rows are the lamp's, the base rows the
    stand's. The rule is attribution's own — a row goes where its KEY is
    counted, not wholesale to whichever product loaded first."""
    lamp, lamp_parts = await _make_product(db_session, ("lid", "printed", 1))
    stand, stand_parts = await _make_product(db_session, ("base", "printed", 1))
    db_session.add_all(
        [
            ProductPlate(product_id=lamp.id, library_file_id=77, plate_index=0),
            ProductPlate(product_id=stand.id, library_file_id=77, plate_index=0),
        ]
    )
    archive = await _archive(db_session, file_id=77)
    await _rows(db_session, archive, ("lid", 2, 0), ("base", 3, 0))

    await credit_unfiled_print(db_session, archive)

    assert await balances(db_session, lamp.id) == {lamp_parts["lid"].id: 2}
    assert await balances(db_session, stand.id) == {stand_parts["base"].id: 3}


async def test_an_exact_plate_link_beats_the_whole_file_wildcard(db_session):
    """``products_for_print`` is not a union: a product that named THIS plate
    takes the print, and the one that claimed the whole file does not also get
    it. Otherwise a shared file would put the same physical parts on two
    shelves."""
    exact, exact_parts = await _make_product(db_session, ("lid", "printed", 1))
    wildcard, wildcard_parts = await _make_product(db_session, ("lid", "printed", 1))
    db_session.add_all(
        [
            ProductPlate(product_id=exact.id, library_file_id=77, plate_index=2),
            ProductPlate(product_id=wildcard.id, library_file_id=77, plate_index=0),
        ]
    )
    archive = await _archive(db_session, file_id=77, plate_index=2)
    await _rows(db_session, archive, ("lid", 6, 0))

    await credit_unfiled_print(db_session, archive)

    assert await balances(db_session, exact.id) == {exact_parts["lid"].id: 6}
    assert await balances(db_session, wildcard.id) == {wildcard_parts["lid"].id: 0}


async def test_filing_the_print_under_an_order_takes_the_credit_back(db_session, shelf):
    """Decision 3's other half. The reversal is a ``manual`` movement carrying
    the same ``archive_id``, so the product's history reads as a pair rather
    than as a balance that changed by itself."""
    product, parts, archive = shelf
    await credit_unfiled_print(db_session, archive)

    written = await reverse_unfiled_print(db_session, archive, note=NOTE_FILED_UNDER_ORDER)

    assert {m.reason for m in written} == {"manual"}
    assert {m.note for m in written} == {NOTE_FILED_UNDER_ORDER}
    assert {m.archive_id for m in written} == {archive.id}
    assert await balances(db_session, product.id) == {parts["lid"].id: 0, parts["base"].id: 0}


async def test_filing_the_same_print_twice_reverses_it_once(db_session, shelf):
    """Idempotent by arithmetic: the sum of everything naming this archive is
    already zero, so there is nothing left to negate."""
    product, parts, archive = shelf
    await credit_unfiled_print(db_session, archive)
    await reverse_unfiled_print(db_session, archive, note=NOTE_FILED_UNDER_ORDER)

    assert await reverse_unfiled_print(db_session, archive, note=NOTE_FILED_UNDER_ORDER) == []
    assert await balances(db_session, product.id) == {parts["lid"].id: 0, parts["base"].id: 0}


async def test_reversing_a_credit_that_has_already_been_spent_is_refused(db_session, shelf):
    """The parts went out to another order between the print and the filing.
    The ledger will not go below zero for it — the archive route catches this,
    logs it and files the archive anyway, because the print history must still
    be correctable."""
    _product, parts, archive = shelf
    await credit_unfiled_print(db_session, archive)
    await move(db_session, part_id=parts["lid"].id, delta=-3, reason="reserved_for_order")

    with pytest.raises(PartStockError):
        await reverse_unfiled_print(db_session, archive, note=NOTE_FILED_UNDER_ORDER)


async def test_a_part_that_stopped_counting_is_skipped_by_the_reversal(db_session, shelf):
    """It has no balance left to take back (``balances`` filters it out), and
    ``move`` would refuse it as a caller error — which would turn a routine
    re-filing into a 500."""
    _product, parts, archive = shelf
    await credit_unfiled_print(db_session, archive)
    parts["lid"].qty_per_unit = 0
    await db_session.flush()

    written = await reverse_unfiled_print(db_session, archive, note=NOTE_FILED_UNDER_ORDER)

    assert [m.product_part_id for m in written] == [parts["base"].id]


async def test_detaching_an_archive_keeps_the_parts_on_the_shelf(db_session, shelf):
    """Spec §Invariants touched: deleting a print does not un-print it. The link
    is cut, the balance stands."""
    product, parts, archive = shelf
    await credit_unfiled_print(db_session, archive)

    assert await detach_archive(db_session, archive.id) == 2

    assert await balances(db_session, product.id) == {parts["lid"].id: 3, parts["base"].id: 4}
    assert {m.archive_id for m in await movements(db_session, product.id)} == {None}


async def test_delete_for_parts_takes_several_ledgers_in_one_statement(db_session):
    """What the product-delete route needs; ``delete_for_part`` one at a time
    would be one statement per part of a product that is going away entirely."""
    product, parts = await _make_product(db_session, ("lid", "printed", 1), ("base", "printed", 1))
    other, other_parts = await _make_product(db_session, ("lid", "printed", 1))
    await move(db_session, part_id=parts["lid"].id, delta=5, reason="unfiled_print")
    await move(db_session, part_id=parts["base"].id, delta=2, reason="unfiled_print")
    kept = await move(db_session, part_id=other_parts["lid"].id, delta=1, reason="unfiled_print")

    assert await delete_for_parts(db_session, [parts["lid"].id, parts["base"].id]) == 2

    assert await movements(db_session, product.id) == []
    assert [m.id for m in await movements(db_session, other.id)] == [kept.id]


async def test_delete_for_parts_asked_for_nothing_touches_nothing(db_session):
    """An empty ``IN ()`` is a statement SQLAlchemy warns about and PostgreSQL
    plans badly; a product with no parts is an ordinary product."""
    _product, parts = await _make_product(db_session, ("lid", "printed", 1))
    await move(db_session, part_id=parts["lid"].id, delta=5, reason="unfiled_print")

    assert await delete_for_parts(db_session, []) == 0
    assert await _row_count(db_session, parts["lid"].id) == 1


# ---------- Ruling 11: un-filing puts the parts back, and the net is the key ----------


async def test_the_net_is_zero_before_anything_and_the_credit_after(db_session, shelf):
    """The one function both the writer's idempotency check and the archive
    route's 409 read, so "is this print already on the shelf" has one answer."""
    _product, _parts, archive = shelf
    assert await unfiled_credit_net(db_session, archive.id) == 0

    await credit_unfiled_print(db_session, archive)

    assert await unfiled_credit_net(db_session, archive.id) == 7  # 3 lids + 4 bases


async def test_a_filed_print_holds_nothing_even_though_its_rows_remain(db_session, shelf):
    """Why the key is the net and not a row existing: after the reversal this
    archive still names four movements and holds none of the stock."""
    _product, _parts, archive = shelf
    await credit_unfiled_print(db_session, archive)
    await reverse_unfiled_print(db_session, archive, note=NOTE_FILED_UNDER_ORDER)

    assert await unfiled_credit_net(db_session, archive.id) == 0
    assert len([m for m in await movements(db_session, _product.id) if m.archive_id == archive.id]) == 4


async def test_un_filing_a_print_puts_its_parts_back_on_the_shelf(db_session, shelf):
    """Ruling 11. Once the order stops counting these parts, nothing does —
    without the re-credit a plate would quietly vanish every time somebody
    corrected a filing."""
    product, parts, archive = shelf
    await credit_unfiled_print(db_session, archive)
    await reverse_unfiled_print(db_session, archive, note=NOTE_FILED_UNDER_ORDER)
    assert await balances(db_session, product.id) == {parts["lid"].id: 0, parts["base"].id: 0}

    # …and the operator takes it back out of the order.
    written = await credit_unfiled_print(db_session, archive)

    assert {m.reason for m in written} == {"unfiled_print"}
    assert await balances(db_session, product.id) == {parts["lid"].id: 3, parts["base"].id: 4}
    assert await unfiled_credit_net(db_session, archive.id) == 7


async def test_the_credit_records_the_operator_who_asked_for_it(db_session, shelf):
    """``created_by`` is the archive page's "count this into stock" button. The
    completion handler passes nothing and writes with no user (Decision 7)."""
    _product, _parts, archive = shelf

    written = await credit_unfiled_print(db_session, archive, created_by=42)

    assert {m.created_by for m in written} == {42}


async def test_the_completion_handlers_credit_names_no_user(db_session, shelf):
    _product, _parts, archive = shelf

    written = await credit_unfiled_print(db_session, archive)

    assert {m.created_by for m in written} == {None}


async def test_the_reversal_finishes_every_part_it_can_before_it_complains(db_session, shelf):
    """Ruling 12a. One part's stock having been spent says nothing about the
    others — stopping at the first refusal would leave the rest of the plate
    double-counted for no reason. The reversals that worked stay written and
    ONE error names what was refused."""
    product, parts, archive = shelf
    await credit_unfiled_print(db_session, archive)
    await move(db_session, part_id=parts["lid"].id, delta=-3, reason="reserved_for_order")

    with pytest.raises(PartStockError) as refusal:
        await reverse_unfiled_print(db_session, archive, note=NOTE_FILED_UNDER_ORDER)

    # The whole id list, so a second refused part would fail this rather than
    # hide inside a message that happens to contain the digit.
    assert f"part(s) {parts['lid'].id} had already spent" in str(refusal.value)
    # The base came back off the shelf even though the lid could not.
    assert await balances(db_session, product.id) == {parts["lid"].id: 0, parts["base"].id: 0}
    assert await unfiled_credit_net(db_session, archive.id) == 3, "the lids the order now double-counts"


# ---------- an order line's reservation (Decision 4) ----------


async def _line(db_session, product, quantity=10) -> ProjectLine:
    """One order line against ``product``, with the order the FK names."""
    project = Project(name="O")
    db_session.add(project)
    await db_session.flush()
    line = ProjectLine(project_id=project.id, product_id=product.id, quantity=quantity, sort_order=0)
    db_session.add(line)
    await db_session.flush()
    return line


async def _line_rows(db_session, line_id: int) -> list[ProductPartStockMovement]:
    rows = await db_session.execute(
        select(ProductPartStockMovement)
        .where(ProductPartStockMovement.project_line_id == line_id)
        .order_by(ProductPartStockMovement.id)
    )
    return list(rows.scalars().all())


async def test_a_line_reserves_whole_kits_across_every_counted_part(db_session, kit):
    """Decision 4: the kit is the unit the operator thinks in. Two kits of a
    one-lid-one-base product take two of each, and what is left is what the
    next line may take."""
    product, parts = kit
    line = await _line(db_session, product)

    assert await reserve_for_line(db_session, line, 2) == 2

    assert await balances(db_session, product.id) == {parts["lid"].id: 3, parts["base"].id: 1}
    assert kits_available(await balances(db_session, product.id), list(parts.values())) == 1
    assert [(m.reason, m.delta) for m in await _line_rows(db_session, line.id)] == [
        ("reserved_for_order", -2),
        ("reserved_for_order", -2),
    ]


async def test_a_reservation_takes_qty_per_unit_of_each_part(db_session):
    """A part wanted twice per unit is reserved twice per kit — the same
    multiplication the ledger has to be read back through."""
    product, parts = await _make_product(db_session, ("shade", "printed", 1), ("arm", "printed", 2))
    await move(db_session, part_id=parts["shade"].id, delta=10, reason="unfiled_print")
    await move(db_session, part_id=parts["arm"].id, delta=10, reason="unfiled_print")
    line = await _line(db_session, product)

    # 10 arms make 5 kits, 10 shades make 10 — the scarcest part decides.
    assert await reserve_for_line(db_session, line, 5) == 5
    assert await balances(db_session, product.id) == {parts["shade"].id: 5, parts["arm"].id: 0}


async def test_asking_for_more_kits_than_the_shelf_holds_reserves_what_is_there(db_session, kit):
    """Ruling 1: the dialog rendered a default and the operator pressed OK some
    minutes later. The honest answer is a smaller reservation, not an error
    page — and the RETURN VALUE is what the response tells the dialog."""
    product, parts = kit
    line = await _line(db_session, product)

    assert await reserve_for_line(db_session, line, 5) == 3, "three bases is three kits"

    assert await balances(db_session, product.id) == {parts["lid"].id: 2, parts["base"].id: 0}
    assert all(row.delta == -3 for row in await _line_rows(db_session, line.id))


async def test_a_product_with_nothing_on_the_shelf_reserves_nothing_and_says_so(db_session):
    product, _parts = await _make_product(db_session, ("lid", "printed", 1))
    line = await _line(db_session, product)

    assert await reserve_for_line(db_session, line, 3) == 0
    assert await _line_rows(db_session, line.id) == [], "a ledger of zeroes is a ledger nobody reads"


async def test_a_product_with_no_counted_part_reserves_nothing(db_session):
    """Nothing to be short of — the same answer ``kits_available`` gives."""
    product, _parts = await _make_product(db_session, ("screw", "purchased", 4), ("jig", "printed", 0))
    line = await _line(db_session, product)

    assert await reserve_for_line(db_session, line, 3) == 0


async def test_rewriting_a_reservation_downwards_releases_only_the_difference(db_session, kit):
    """Editing 3 to 1 puts two kits back. The ledger keeps both movements —
    what went out and what came back, never a row edited in place."""
    product, parts = kit
    line = await _line(db_session, product)
    await reserve_for_line(db_session, line, 3)

    assert await reserve_for_line(db_session, line, 1) == 1

    assert await balances(db_session, product.id) == {parts["lid"].id: 4, parts["base"].id: 2}
    lid_rows = [m for m in await _line_rows(db_session, line.id) if m.product_part_id == parts["lid"].id]
    assert [(m.reason, m.delta) for m in lid_rows] == [
        ("reserved_for_order", -3),
        ("reservation_released", 3),
        ("reserved_for_order", -1),
    ]


async def test_rewriting_a_reservation_to_the_same_number_keeps_it(db_session, kit):
    """The release comes FIRST or the line bids against itself: the product's
    balance already has this line's own kits subtracted from it, so computing
    ``kits_available`` before releasing would turn an edit from 3 to 3 into a
    reservation of nothing."""
    product, parts = kit
    line = await _line(db_session, product)
    await reserve_for_line(db_session, line, 3)

    assert await reserve_for_line(db_session, line, 3) == 3

    assert await balances(db_session, product.id) == {parts["lid"].id: 2, parts["base"].id: 0}


async def test_a_reservation_of_zero_releases_the_whole_thing(db_session, kit):
    product, parts = kit
    line = await _line(db_session, product)
    await reserve_for_line(db_session, line, 3)

    assert await reserve_for_line(db_session, line, 0) == 0

    assert await balances(db_session, product.id) == {parts["lid"].id: 5, parts["base"].id: 3}


async def test_a_negative_reservation_is_a_caller_bug(db_session, kit):
    product, _parts = kit
    line = await _line(db_session, product)
    with pytest.raises(ValueError, match="never negative"):
        await reserve_for_line(db_session, line, -1)


async def test_a_reservation_never_names_an_archive(db_session, kit):
    """``reverse_unfiled_print`` negates EVERY row carrying an archive id, so a
    reservation caught in that sum would be handed back as stock a print never
    made."""
    product, _parts = kit
    line = await _line(db_session, product)
    await reserve_for_line(db_session, line, 2)

    assert {m.archive_id for m in await _line_rows(db_session, line.id)} == {None}


async def test_releasing_reads_the_ledger_and_not_the_line(db_session, kit):
    """What comes back is what went out — even after the line's quantity has
    been edited. Recomputing ``quantity * qty_per_unit`` is how a ledger starts
    inventing stock."""
    product, parts = kit
    line = await _line(db_session, product, quantity=10)
    await reserve_for_line(db_session, line, 2)
    line.quantity = 99
    await db_session.flush()

    await release_for_line(db_session, line, note=NOTE_ORDER_CANCELLED)

    assert await balances(db_session, product.id) == {parts["lid"].id: 5, parts["base"].id: 3}
    released = [m for m in await _line_rows(db_session, line.id) if m.reason == "reservation_released"]
    assert [m.delta for m in released] == [2, 2]
    assert {m.note for m in released} == {NOTE_ORDER_CANCELLED}


async def test_releasing_a_line_that_reserved_nothing_writes_nothing(db_session, kit):
    product, _parts = kit
    line = await _line(db_session, product)

    await release_for_line(db_session, line, note=NOTE_ORDER_CANCELLED)

    assert await _line_rows(db_session, line.id) == []


async def test_releasing_twice_gives_the_shelf_its_kits_back_once(db_session, kit):
    """Cancelling an order that was already cancelled, or deleting a line of
    it: the second pass reads the ledger, finds nothing outstanding and writes
    nothing."""
    product, parts = kit
    line = await _line(db_session, product)
    await reserve_for_line(db_session, line, 3)
    await release_for_line(db_session, line, note=NOTE_ORDER_CANCELLED)

    await release_for_line(db_session, line, note=NOTE_LINE_DELETED)

    assert await balances(db_session, product.id) == {parts["lid"].id: 5, parts["base"].id: 3}
    assert [m.reason for m in await _line_rows(db_session, line.id)].count("reservation_released") == 2


async def test_a_part_that_stopped_counting_keeps_its_reservation_out(db_session, kit):
    """It has no balance to return to, and ``move`` would refuse it as a caller
    error. The rest of the kit still comes back — one part is not the plate."""
    product, parts = kit
    line = await _line(db_session, product)
    await reserve_for_line(db_session, line, 2)
    parts["lid"].kind = "purchased"
    await db_session.flush()

    await release_for_line(db_session, line, note=NOTE_ORDER_CANCELLED)

    assert await balances(db_session, product.id) == {parts["base"].id: 3}


async def test_the_reservation_reads_back_as_kits_for_many_lines_in_one_query(db_session, test_engine):
    """The loaders ask about a whole page of orders at once (the pass-6 batch
    discipline), so this is ONE statement however many lines there are."""
    product, parts = await _make_product(db_session, ("shade", "printed", 1), ("arm", "printed", 2))
    await move(db_session, part_id=parts["shade"].id, delta=20, reason="unfiled_print")
    await move(db_session, part_id=parts["arm"].id, delta=20, reason="unfiled_print")
    first = await _line(db_session, product)
    second = await _line(db_session, product)
    empty = await _line(db_session, product)
    await reserve_for_line(db_session, first, 3)
    await reserve_for_line(db_session, second, 2)
    qty = {parts["shade"].id: 1, parts["arm"].id: 2}

    with counting_statements(test_engine, match="product_part_stock_movements") as seen:
        read = await reserved_units_by_line(db_session, [first.id, second.id, empty.id], qty)

    assert read == {first.id: 3, second.id: 2}, "a line holding nothing is absent, not zero"
    assert len(seen) == 1, "one query for every line of the page"


async def test_the_reading_is_the_net_of_reserve_and_release(db_session, kit):
    """A release is a movement of its own, not the deletion of a reservation —
    so "still reserved" is the sum of both and never "the last row wins"."""
    product, parts = kit
    line = await _line(db_session, product)
    await reserve_for_line(db_session, line, 3)
    await reserve_for_line(db_session, line, 1)

    qty = {part.id: part.qty_per_unit for part in parts.values()}
    assert await reserved_units_by_line(db_session, [line.id], qty) == {line.id: 1}


async def test_the_scarcest_part_decides_what_a_line_holds(db_session, kit):
    """The mirror of ``kits_available``: a hand correction that hands back one
    base of a two-kit reservation leaves the line holding one kit, not two."""
    product, parts = kit
    line = await _line(db_session, product)
    await reserve_for_line(db_session, line, 2)
    await move(db_session, part_id=parts["base"].id, delta=1, reason="reservation_released", project_line_id=line.id)

    qty = {parts["lid"].id: 1, parts["base"].id: 1}
    assert await reserved_units_by_line(db_session, [line.id], qty) == {line.id: 1}


async def test_asking_about_no_lines_asks_the_database_nothing(db_session):
    assert await reserved_units_by_line(db_session, [], {}) == {}


async def test_a_deleted_line_leaves_its_history_behind_with_no_line(db_session, kit):
    """Ruling 10: SQLite honours no ``ON DELETE SET NULL``, so the route detaches
    by hand — AFTER releasing, or the reservation would be invisible to the
    query that hands it back. What survives is the banked history, unlinked."""
    product, parts = kit
    line = await _line(db_session, product)
    await move(db_session, part_id=parts["lid"].id, delta=4, reason="surplus_banked", project_line_id=line.id)
    await reserve_for_line(db_session, line, 2)

    await release_for_line(db_session, line, note=NOTE_LINE_DELETED)
    assert await detach_line(db_session, line.id) == 5

    assert await _line_rows(db_session, line.id) == []
    banked = [m for m in await movements(db_session, product.id) if m.reason == "surplus_banked"]
    assert [(m.delta, m.project_line_id) for m in banked] == [(4, None)]
    # ...and the kits are back on the shelf, which is what "release first" buys.
    assert await balances(db_session, product.id) == {parts["lid"].id: 9, parts["base"].id: 3}


async def test_detaching_a_line_nothing_ever_reserved_moves_no_row(db_session):
    assert await detach_line(db_session, 9999) == 0


# ---------- fix round 1: Ruling 16, Ruling 17, finding I1 ----------


async def test_a_reservation_never_exceeds_the_lines_quantity(db_session, kit):
    """Ruling 16. Three kits are on the shelf and the line orders two, so two
    is what it may hold — kits a line cannot use are stock withheld from every
    other order for nothing."""
    product, parts = kit
    line = await _line(db_session, product, quantity=2)

    assert await reserve_for_line(db_session, line, 5) == 2

    assert await balances(db_session, product.id) == {parts["lid"].id: 3, parts["base"].id: 1}


async def test_the_line_quantity_clamps_before_the_shelf_does(db_session, kit):
    """Both clamps are live at once and the smaller wins whichever it is: five
    on the shelf but only three kits' worth of bases, against a line of ten."""
    product, _parts = kit
    line = await _line(db_session, product, quantity=10)

    assert await reserve_for_line(db_session, line, 10) == 3


async def test_reserved_units_for_line_is_the_one_derivation_of_kits_held(db_session, kit):
    """The route that lowers a quantity asks this; a private SELECT there would
    be a second answer to "how many kits is this line holding"."""
    product, parts = kit
    line = await _line(db_session, product)

    assert await reserved_units_for_line(db_session, line) == 0
    await reserve_for_line(db_session, line, 2)
    assert await reserved_units_for_line(db_session, line) == 2
    await release_for_line(db_session, line, note=NOTE_LINE_DELETED)
    assert await reserved_units_for_line(db_session, line) == 0
    # …and it agrees with the batch reader it is a wrapper over.
    qty = {part.id: part.qty_per_unit for part in parts.values()}
    assert await reserved_units_by_line(db_session, [line.id], qty) == {}


async def test_a_product_with_no_counted_part_holds_no_kits(db_session):
    product, _parts = await _make_product(db_session, ("screw", "purchased", 4))
    line = await _line(db_session, product)

    assert await reserved_units_for_line(db_session, line) == 0


def _normalise(sql: str) -> str:
    return " ".join(sql.split())


async def test_the_kit_decision_is_made_with_every_part_locked(db_session, test_engine, kit):
    """Finding I1. ``move`` locks the ONE part it is about to write, but the kit
    decision is taken across all of them at once — two transactions would
    otherwise each read the same shelf, each decide three kits are free, and the
    per-part clamp would fire on a decision already made.

    The lock statements are ``lock_part_stmt``'s own, compiled: the loop must be
    that statement and not a lookalike, or PostgreSQL emits no ``FOR UPDATE``.
    They are taken in part-id order so two of these loops can never take the
    same two locks the other way round.
    """
    product, parts = kit
    line = await _line(db_session, product)
    wanted = _normalise(str(lock_part_stmt(parts["lid"].id).compile(dialect=sqlite.dialect())))

    with counting_statements(test_engine) as seen:
        await reserve_for_line(db_session, line, 2)

    statements = [_normalise(s) for s in seen]
    balance = next(i for i, s in enumerate(statements) if "LEFT OUTER JOIN product_part_stock_movements" in s)
    assert statements[:balance].count(wanted) == 2, "both counted parts locked before the shelf was read"
    # ⚠️ The shape above is SQLite's, and SQLite's dialect DROPS ``FOR UPDATE``
    # — so on its own it would pass just as happily for a loop that had lost
    # its ``.with_for_update()`` and taken no lock at all. The same statement
    # compiled for PostgreSQL is the only place the lock is visible, and it is
    # the same statement: ``wanted`` is built from ``lock_part_stmt`` too.
    assert "FOR UPDATE" in str(lock_part_stmt(parts["lid"].id).compile(dialect=postgresql.dialect()))


async def test_every_note_the_writer_writes_is_a_listed_token(db_session):
    """Ruling 17. The set is closed: a backend note is a token the product page
    translates, never an English sentence written once and read forever. Both
    directions, so neither an unlisted token nor a dead listing can survive."""
    constants = {
        name: value
        for name, value in vars(part_stock_module).items()
        if name.startswith("NOTE_") and name != "NOTE_TOKENS"
    }
    assert set(constants.values()) == set(NOTE_TOKENS)
    assert len(NOTE_TOKENS) == len(set(NOTE_TOKENS)), "a token listed twice is a token nobody notices"
    assert all(token.islower() and " " not in token for token in NOTE_TOKENS), "tokens, not sentences"


async def test_the_rewrite_note_is_the_token_the_writer_owns(db_session, kit):
    """``reserve_for_line`` writes its own release note; the route never sees it."""
    product, _parts = kit
    line = await _line(db_session, product)
    await reserve_for_line(db_session, line, 3)

    await reserve_for_line(db_session, line, 1)

    released = [m for m in await _line_rows(db_session, line.id) if m.reason == "reservation_released"]
    assert {m.note for m in released} == {NOTE_RESERVATION_REWRITTEN}
