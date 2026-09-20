"""Per-part defect entry through the archive API."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart
from backend.app.models.product import Product, ProductPart, ProductPlate
from backend.app.services.part_stock import balances, credit_unfiled_print

pytestmark = pytest.mark.integration


async def _archive_with_parts(db_session, printer_id: int, parts: dict[str, int]) -> PrintArchive:
    archive = PrintArchive(
        printer_id=printer_id,
        filename="a.3mf",
        print_name="A",
        file_path="x/a.3mf",
        file_size=1,
        status="completed",
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(archive)
    await db_session.flush()
    for name, qty in parts.items():
        db_session.add(
            PrintArchivePart(
                archive_id=archive.id,
                name=name,
                name_key=name.lower(),
                identify_ids=list(range(qty)),
                quantity=qty,
            )
        )
    await db_session.commit()
    await db_session.refresh(archive)
    return archive


@pytest.mark.asyncio
async def test_detail_response_carries_the_part_rows(async_client, printer_factory, db_session):
    printer = await printer_factory()
    archive = await _archive_with_parts(db_session, printer.id, {"lid": 2, "base": 4})

    resp = await async_client.get(f"/api/v1/archives/{archive.id}")

    assert resp.status_code == 200
    parts = {p["name_key"]: p for p in resp.json()["parts"]}
    assert parts["lid"]["quantity"] == 2
    assert parts["base"]["defective"] == 0


@pytest.mark.asyncio
async def test_patching_per_part_defects_derives_the_flat_sum(async_client, printer_factory, db_session):
    printer = await printer_factory()
    archive = await _archive_with_parts(db_session, printer.id, {"lid": 2, "base": 4})
    rows = (
        (await db_session.execute(select(PrintArchivePart).where(PrintArchivePart.archive_id == archive.id)))
        .scalars()
        .all()
    )
    lid = next(r for r in rows if r.name_key == "lid")

    resp = await async_client.patch(
        f"/api/v1/archives/{archive.id}",
        json={"parts_defective": [{"id": lid.id, "defective": 2}]},
    )

    assert resp.status_code == 200
    await db_session.refresh(archive)
    assert archive.defective_count == 2


@pytest.mark.asyncio
async def test_a_defective_above_quantity_is_capped(async_client, printer_factory, db_session):
    printer = await printer_factory()
    archive = await _archive_with_parts(db_session, printer.id, {"lid": 2})
    row = (
        (await db_session.execute(select(PrintArchivePart).where(PrintArchivePart.archive_id == archive.id)))
        .scalars()
        .one()
    )

    resp = await async_client.patch(
        f"/api/v1/archives/{archive.id}",
        json={"parts_defective": [{"id": row.id, "defective": 99}]},
    )

    assert resp.status_code == 200
    await db_session.refresh(row)
    assert row.defective == 2


@pytest.mark.asyncio
async def test_a_foreign_part_row_id_is_rejected(async_client, printer_factory, db_session):
    printer = await printer_factory()
    mine = await _archive_with_parts(db_session, printer.id, {"lid": 2})
    other = await _archive_with_parts(db_session, printer.id, {"base": 1})
    other_row = (
        (await db_session.execute(select(PrintArchivePart).where(PrintArchivePart.archive_id == other.id)))
        .scalars()
        .one()
    )

    resp = await async_client.patch(
        f"/api/v1/archives/{mine.id}",
        json={"parts_defective": [{"id": other_row.id, "defective": 1}]},
    )

    assert resp.status_code == 200, "foreign ids are ignored, not an error"
    await db_session.refresh(other_row)
    assert other_row.defective == 0


@pytest.mark.asyncio
async def test_patching_defects_on_a_credited_print_corrects_the_shelf(async_client, printer_factory, db_session):
    """The archive editor was the one door that wrote defects without the ledger
    hearing about it. A print credited as 2 lids, then marked 1 bad, must leave
    1 lid on the shelf."""
    printer = await printer_factory()
    product = Product(name="Widget")
    db_session.add(product)
    await db_session.flush()
    lid = ProductPart(product_id=product.id, kind="printed", name="lid", name_key="lid", qty_per_unit=1, sort_order=0)
    db_session.add_all([lid, ProductPlate(product_id=product.id, library_file_id=77, plate_index=0)])
    await db_session.flush()
    product_id, lid_id = product.id, lid.id
    archive = await _archive_with_parts(db_session, printer.id, {"lid": 2})
    archive.library_file_id = 77
    archive.plate_index = 1
    await db_session.commit()
    archive_id = archive.id
    await credit_unfiled_print(db_session, archive)
    await db_session.commit()
    assert await balances(db_session, product_id) == {lid_id: 2}
    row = (
        await db_session.execute(select(PrintArchivePart).where(PrintArchivePart.archive_id == archive_id))
    ).scalar_one()

    resp = await async_client.patch(
        f"/api/v1/archives/{archive_id}", json={"parts_defective": [{"id": row.id, "defective": 1}]}
    )

    assert resp.status_code == 200, resp.text
    # ``expire_all`` forces the next reads off the DB (the API used a different
    # session) — but only via a fresh query, never a stale attribute: an
    # expired instance's attribute cannot be lazy-loaded outside the async
    # greenlet (``MissingGreenlet``), so the ids used below were captured
    # while the instances were still fresh.
    db_session.expire_all()
    assert await balances(db_session, product_id) == {lid_id: 1}
    assert (await db_session.get(PrintArchive, archive_id)).defective_count == 1


@pytest.mark.asyncio
async def test_a_flat_count_sent_alone_on_a_print_with_rows_is_ignored(async_client, printer_factory, db_session):
    """Spec §3 rule 1, pinned because the change is silent.

    ``defective_count`` used to be ``setattr`` verbatim; it now goes through the
    defects writer as ``DefectsWrite.flat``, which ignores a flat value whenever
    the print HAS part rows — the column becomes ``Σ rows.defective``. The
    in-app editor never sends it that way (it sends the flat value only when
    there are no rows), but an API-key client or an older bundle would have had
    its number replaced, and nothing said so.
    """
    printer = await printer_factory()
    archive = await _archive_with_parts(db_session, printer.id, {"lid": 2, "base": 4})
    row = (
        await db_session.execute(
            select(PrintArchivePart).where(
                PrintArchivePart.archive_id == archive.id, PrintArchivePart.name_key == "lid"
            )
        )
    ).scalar_one()
    row.defective = 1
    await db_session.commit()
    archive_id = archive.id

    resp = await async_client.patch(f"/api/v1/archives/{archive_id}", json={"defective_count": 99})

    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    assert (await db_session.get(PrintArchive, archive_id)).defective_count == 1, "the rows are the truth, not the 99"


@pytest.mark.asyncio
async def test_a_null_flat_count_leaves_the_column_untouched(async_client, printer_factory, db_session):
    """``defective_count: null`` used to null the column. It is now skipped —
    the writer has no "unknown" to write, and a rowless print's recorded scrap
    must not be wiped by a PATCH that happens to carry the key as null.
    """
    printer = await printer_factory()
    archive = await _archive_with_parts(db_session, printer.id, {})
    archive.quantity = 4
    archive.defective_count = 2
    await db_session.commit()
    archive_id = archive.id

    resp = await async_client.patch(f"/api/v1/archives/{archive_id}", json={"defective_count": None})

    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    assert (await db_session.get(PrintArchive, archive_id)).defective_count == 2
