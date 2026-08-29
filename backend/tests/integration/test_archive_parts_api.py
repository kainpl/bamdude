"""Per-part defect entry through the archive API."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart

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
