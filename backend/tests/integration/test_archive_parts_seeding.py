"""Seeding print_archive_parts wherever an archive gains its 3MF.

The rows are the live per-part state of the plate: written at print start,
intersected by skips, edited by the defect dialog. Seeding is best-effort —
a print must never fail because of the ledger.
"""

import io
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart
from backend.app.services.archive_parts import seed_archive_parts

pytestmark = pytest.mark.integration


def _3mf(objects: dict[int, str], plate: int = 1) -> bytes:
    entries = "".join(f'<object identify_id="{oid}" name="{name}" skipped="false"/>' for oid, name in objects.items())
    slice_info = f'<config><plate><metadata key="index" value="{plate}"/>{entries}</plate></config>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Metadata/slice_info.config", slice_info)
    return buf.getvalue()


async def _archive(db, *, plate_index=1, defective=0) -> PrintArchive:
    archive = PrintArchive(
        printer_id=1,
        filename="seed.3mf",
        print_name="Seed",
        file_path="x/seed.3mf",
        file_size=1,
        status="printing",
        plate_index=plate_index,
        defective_count=defective,
        started_at=datetime.now(timezone.utc),
    )
    db.add(archive)
    await db.commit()
    await db.refresh(archive)
    return archive


async def _rows(db, archive_id):
    return (await db.execute(select(PrintArchivePart).where(PrintArchivePart.archive_id == archive_id))).scalars().all()


@pytest.mark.asyncio
async def test_seeding_groups_instances_by_canonical_name(db_session):
    archive = await _archive(db_session)
    await seed_archive_parts(db_session, archive, _3mf({941: "part.stl_1", 942: "part.stl_2", 943: "lid"}))
    await db_session.commit()

    rows = {r.name_key: r for r in await _rows(db_session, archive.id)}
    assert rows["part.stl"].quantity == 2
    assert sorted(rows["part.stl"].identify_ids) == [941, 942]
    assert rows["lid"].quantity == 1
    assert rows["lid"].defective == 0


@pytest.mark.asyncio
async def test_reseed_replaces_rows_but_carries_defective_by_name(db_session):
    """Plate-corrected re-download: parts still present keep recorded scrap."""
    archive = await _archive(db_session)
    await seed_archive_parts(db_session, archive, _3mf({1: "lid", 2: "lid", 3: "gone.stl"}))
    await db_session.commit()
    lid = next(r for r in await _rows(db_session, archive.id) if r.name_key == "lid")
    lid.defective = 2
    await db_session.commit()

    await seed_archive_parts(db_session, archive, _3mf({7: "lid", 8: "new.stl"}))
    await db_session.commit()

    rows = {r.name_key: r for r in await _rows(db_session, archive.id)}
    assert set(rows) == {"lid", "new.stl"}
    assert rows["lid"].defective == 1, "carried over but capped at the new quantity"
    assert rows["lid"].identify_ids == [7]


@pytest.mark.asyncio
async def test_a_file_without_objects_seeds_nothing(db_session):
    archive = await _archive(db_session)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Metadata/other.txt", "no slice info")
    await seed_archive_parts(db_session, archive, buf.getvalue())
    await db_session.commit()

    assert await _rows(db_session, archive.id) == []


@pytest.mark.asyncio
async def test_garbage_bytes_do_not_raise(db_session):
    """Best-effort: the ledger must never fail the print path."""
    archive = await _archive(db_session)
    await seed_archive_parts(db_session, archive, b"not a zip at all")
    await db_session.commit()

    assert await _rows(db_session, archive.id) == []


@pytest.mark.asyncio
async def test_a_path_that_does_not_exist_does_not_raise(db_session):
    """The Path form does the read INSIDE the guard — a transient/missing
    file must never raise out of seed_archive_parts and into the caller
    (print dispatch, 3MF attach)."""
    archive = await _archive(db_session)
    await seed_archive_parts(db_session, archive, Path("Z:/definitely/not/there.3mf"))
    await db_session.commit()

    assert await _rows(db_session, archive.id) == []


@pytest.mark.asyncio
async def test_archive_print_seeds_rows(db_session, tmp_path, printer_factory):
    from backend.app.services.archive import ArchiveService

    printer = await printer_factory()
    f = tmp_path / "job.gcode.3mf"
    f.write_bytes(_3mf({1: "part.stl_1", 2: "part.stl_2"}))

    service = ArchiveService(db_session)
    archive = await service.archive_print(printer.id, f, plate_index=1)
    await db_session.commit()

    assert archive is not None
    rows = await _rows(db_session, archive.id)
    assert len(rows) == 1 and rows[0].quantity == 2


@pytest.mark.asyncio
async def test_attach_seeds_rows_on_a_fallback_archive(db_session, tmp_path, printer_factory):
    from backend.app.services.archive import ArchiveService

    printer = await printer_factory()
    archive = PrintArchive(
        printer_id=printer.id,
        filename="late.3mf",
        print_name="Late",
        file_path="",
        file_size=0,
        status="printing",
        plate_index=1,
        started_at=datetime.now(timezone.utc),
        extra_data={"no_3mf_available": True},
    )
    db_session.add(archive)
    await db_session.commit()
    await db_session.refresh(archive)

    f = tmp_path / "late.gcode.3mf"
    f.write_bytes(_3mf({5: "lid"}))
    ok = await ArchiveService(db_session).attach_3mf_to_archive(archive.id, f)
    await db_session.commit()

    assert ok
    rows = await _rows(db_session, archive.id)
    assert [r.name_key for r in rows] == ["lid"]


@pytest.mark.asyncio
async def test_a_print_that_finished_before_its_3mf_arrived_still_reaches_the_shelf(
    db_session, tmp_path, printer_factory
):
    """Ruling 27. An external print reaches ``completed`` before its file does:
    the completion hook ran against an archive with no part rows at all and
    credited nothing, and the four triggers that finally attach the 3MF only
    seed the rows. Nothing re-ran the credit, so a print that arrived by that
    door never reached the product's free stock.

    The credit is idempotent per part, so attaching twice — a plate-corrected
    re-download, a retry sweep landing on an archive the reconnect already
    fixed — moves nothing the second time.
    """
    from backend.app.models.part_stock import ProductPartStockMovement
    from backend.app.models.product import Product, ProductPart, ProductPlate
    from backend.app.services import part_stock
    from backend.app.services.archive import ArchiveService

    printer = await printer_factory()
    product = Product(name="Lamp")
    db_session.add(product)
    await db_session.flush()
    lid = ProductPart(product_id=product.id, kind="printed", name="lid", name_key="lid", qty_per_unit=1)
    db_session.add_all([lid, ProductPlate(product_id=product.id, library_file_id=901, plate_index=0)])
    archive = PrintArchive(
        printer_id=printer.id,
        filename="late.3mf",
        print_name="Late",
        file_path="",
        file_size=0,
        status="completed",
        library_file_id=901,
        plate_index=1,
        started_at=datetime.now(timezone.utc),
        extra_data={"no_3mf_available": True},
    )
    db_session.add(archive)
    await db_session.commit()
    product_id, lid_id, archive_id = product.id, lid.id, archive.id
    f = tmp_path / "late.gcode.3mf"
    f.write_bytes(_3mf({5: "lid", 6: "lid"}))

    assert await ArchiveService(db_session).attach_3mf_to_archive(archive_id, f)

    assert await part_stock.balances(db_session, product_id) == {lid_id: 2}
    assert await part_stock.unfiled_credit_net(db_session, archive_id) == 2

    assert await ArchiveService(db_session).attach_3mf_to_archive(archive_id, f)

    assert await part_stock.balances(db_session, product_id) == {lid_id: 2}, "a second attach credits nothing more"
    rows = (await db_session.execute(select(ProductPartStockMovement))).scalars().all()
    assert [(r.reason, r.delta) for r in rows] == [("unfiled_print", 2)]


@pytest.mark.asyncio
async def test_a_credit_that_falls_over_leaves_the_attach_green(db_session, tmp_path, printer_factory, monkeypatch):
    """The credit is optional; the file is not.

    ``credit_if_unfiled`` swallows whatever the ledger objects to, but that is
    only half of "never fails its caller": a failure that got as far as the
    DATABASE takes the session's transaction down with it, and by the time the
    polite empty list comes back, the copied file and every field written above
    it are gone. So the credit runs inside a SAVEPOINT — the ledger fails in its
    own scope, and the attach still commits.

    The stand-in flushes a row BEFORE it raises, which is the whole point: a
    credit that raised before touching the session would prove nothing.
    """
    from backend.app.models.part_stock import ProductPartStockMovement
    from backend.app.models.product import Product, ProductPart, ProductPlate
    from backend.app.services import part_stock
    from backend.app.services.archive import ArchiveService

    printer = await printer_factory()
    product = Product(name="Lamp")
    db_session.add(product)
    await db_session.flush()
    lid = ProductPart(product_id=product.id, kind="printed", name="lid", name_key="lid", qty_per_unit=1)
    db_session.add_all([lid, ProductPlate(product_id=product.id, library_file_id=903, plate_index=0)])
    archive = PrintArchive(
        printer_id=printer.id,
        filename="late.3mf",
        print_name="Late",
        file_path="",
        file_size=0,
        status="completed",
        library_file_id=903,
        plate_index=1,
        started_at=datetime.now(timezone.utc),
        extra_data={"no_3mf_available": True},
    )
    db_session.add(archive)
    await db_session.commit()
    product_id, lid_id, archive_id = product.id, lid.id, archive.id
    f = tmp_path / "late.gcode.3mf"
    f.write_bytes(_3mf({5: "lid"}))

    async def _flush_then_explode(db, _archive, **_kwargs):
        db.add(ProductPartStockMovement(product_part_id=lid_id, delta=1, reason="unfiled_print"))
        await db.flush()
        raise RuntimeError("the ledger fell over mid-write")

    monkeypatch.setattr(part_stock, "credit_unfiled_print", _flush_then_explode)

    assert await ArchiveService(db_session).attach_3mf_to_archive(archive_id, f)

    db_session.expire_all()
    attached = await db_session.get(PrintArchive, archive_id)
    assert attached.file_path, "the attach committed"
    assert await _rows(db_session, archive_id), "and seeded its parts"
    assert await part_stock.balances(db_session, product_id) == {lid_id: 0}
    rows = (await db_session.execute(select(ProductPartStockMovement))).scalars().all()
    assert rows == [], "the half-written credit went back with its savepoint"


@pytest.mark.asyncio
async def test_attaching_a_3mf_to_a_print_filed_under_an_order_credits_nothing(db_session, tmp_path, printer_factory):
    """The other half of the same guard: an order's print is counted by the
    order, and putting it on the free shelf as well would count it twice."""
    from backend.app.models.part_stock import ProductPartStockMovement
    from backend.app.models.product import Product, ProductPart, ProductPlate
    from backend.app.models.project import Project
    from backend.app.services.archive import ArchiveService

    printer = await printer_factory()
    product = Product(name="Lamp")
    project = Project(name="Order")
    db_session.add_all([product, project])
    await db_session.flush()
    db_session.add_all(
        [
            ProductPart(product_id=product.id, kind="printed", name="lid", name_key="lid", qty_per_unit=1),
            ProductPlate(product_id=product.id, library_file_id=902, plate_index=0),
        ]
    )
    archive = PrintArchive(
        printer_id=printer.id,
        project_id=project.id,
        filename="filed.3mf",
        print_name="Filed",
        file_path="",
        file_size=0,
        status="completed",
        library_file_id=902,
        plate_index=1,
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(archive)
    await db_session.commit()
    archive_id = archive.id
    f = tmp_path / "filed.gcode.3mf"
    f.write_bytes(_3mf({5: "lid"}))

    assert await ArchiveService(db_session).attach_3mf_to_archive(archive_id, f)

    assert (await db_session.execute(select(ProductPartStockMovement))).scalars().all() == []


def test_flat_defective_attributes_only_on_a_mono_part_plate():
    from backend.app.services.archive_parts import apply_flat_defective

    lid = PrintArchivePart(archive_id=1, name="lid", name_key="lid", identify_ids=[1, 2, 3], quantity=3)
    assert apply_flat_defective([lid], 2) is True
    assert lid.defective == 2

    lid2 = PrintArchivePart(archive_id=1, name="lid", name_key="lid", identify_ids=[1], quantity=1)
    assert apply_flat_defective([lid2], 5) is True
    assert lid2.defective == 1, "capped at quantity"

    a = PrintArchivePart(archive_id=1, name="a", name_key="a", identify_ids=[1], quantity=1)
    b = PrintArchivePart(archive_id=1, name="b", name_key="b", identify_ids=[2], quantity=1)
    assert apply_flat_defective([a, b], 1) is False, "multi-part plates stay unattributed"
    assert (a.defective or 0) == 0 and (b.defective or 0) == 0

    assert apply_flat_defective([], 3) is False
