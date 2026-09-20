"""One writer for what came out bad on a plate — and the shelf follows it.

Built on the ledger tests' own fixtures: a product whose single-plate file
yields lids and bases, and one finished order-less print of it.
"""

from sqlalchemy import select

from backend.app.models.archive_part import PrintArchivePart
from backend.app.services.archive_defects import DefectsWrite, record_defects
from backend.app.services.part_stock import NOTE_DEFECTS_RECORDED, balances, credit_unfiled_print, move
from backend.tests.unit.services.test_part_stock import _archive, _make_product, shelf  # noqa: F401 — pytest fixture


async def _rows_of(db_session, archive) -> dict[str, PrintArchivePart]:
    rows = (
        (await db_session.execute(select(PrintArchivePart).where(PrintArchivePart.archive_id == archive.id)))
        .scalars()
        .all()
    )
    return {row.name_key: row for row in rows}


async def test_per_part_values_are_absolute_clamped_and_summed(db_session, shelf):
    _product, _parts, archive = shelf
    rows = await _rows_of(db_session, archive)

    result = await record_defects(
        db_session, archive, DefectsWrite(parts=((rows["lid"].id, 99), (rows["base"].id, 1), (999_999, 5)))
    )

    assert {r.name_key: r.defective for r in result.parts} == {"lid": 4, "base": 1}
    assert result.defective_count == 5 and archive.defective_count == 5


async def test_flat_is_ignored_when_rows_exist_and_clamped_when_they_do_not(db_session, shelf):
    _product, _parts, archive = shelf
    result = await record_defects(db_session, archive, DefectsWrite(flat=3))
    assert result.defective_count == 1, "rows exist: the flat value is not the truth, their sum is"

    bare = await _archive(db_session, file_id=77)
    bare.quantity = 4
    result = await record_defects(db_session, bare, DefectsWrite(flat=9))
    assert result.defective_count == 4 and bare.defective_count == 4
    result = await record_defects(db_session, bare, DefectsWrite(flat=-2))
    assert result.defective_count == 0


async def test_the_shelf_follows_an_order_less_completed_print(db_session, shelf):
    product, parts, archive = shelf
    await credit_unfiled_print(db_session, archive)
    rows = await _rows_of(db_session, archive)

    result = await record_defects(db_session, archive, DefectsWrite(parts=((rows["lid"].id, 2),)))

    assert result.ledger_adjusted == [(parts["lid"].id, -1)] and result.ledger_refused == []
    assert await balances(db_session, product.id) == {parts["lid"].id: 2, parts["base"].id: 4}


async def test_a_refused_correction_keeps_the_defects_and_names_the_part(db_session, shelf):
    product, parts, archive = shelf
    await credit_unfiled_print(db_session, archive)
    await move(db_session, part_id=parts["lid"].id, delta=-3, reason="manual", note="sold")
    rows = await _rows_of(db_session, archive)

    result = await record_defects(db_session, archive, DefectsWrite(parts=((rows["lid"].id, 2),)))

    assert result.defective_count == 2, "the archive says what came out bad whatever the shelf can do"
    assert result.ledger_refused == [parts["lid"].id] and result.ledger_adjusted == []


async def test_a_filed_print_writes_no_ledger_row(db_session, shelf):
    product, parts, archive = shelf
    archive.project_id = 1
    rows = await _rows_of(db_session, archive)

    result = await record_defects(db_session, archive, DefectsWrite(parts=((rows["lid"].id, 2),)))

    assert result.defective_count == 2 and result.ledger_adjusted == []
    assert await balances(db_session, product.id) == {parts["lid"].id: 0, parts["base"].id: 0}
