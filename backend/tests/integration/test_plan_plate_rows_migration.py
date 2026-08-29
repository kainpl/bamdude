"""m158 part 2: plan rows become per-plate.

plate_index 0 = the whole file (single-plate files, raw gcode); 1..N = that
plate of a multi-plate 3MF. The seed expands legacy rows: a multi-plate
file's single row becomes one row per plate, each inheriting copies (the
user's decision — today's copies means "N x the whole file", and totals
multiply whole-file metadata by copies, so per-plate inheritance keeps
every sum identical).
"""

import io
import zipfile
from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.migrations import m158_parts_ledger
from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart
from backend.app.models.library import LibraryFile
from backend.app.models.project import Project
from backend.app.models.project_print_plan import ProjectPrintPlanItem

pytestmark = pytest.mark.integration


def _3mf(objects: dict[int, str], plate: int = 1) -> bytes:
    entries = "".join(f'<object identify_id="{oid}" name="{name}" skipped="false"/>' for oid, name in objects.items())
    slice_info = f'<config><plate><metadata key="index" value="{plate}"/>{entries}</plate></config>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Metadata/slice_info.config", slice_info)
    return buf.getvalue()


def test_model_has_plate_index_and_the_widened_unique():
    cols = {c.name for c in inspect(ProjectPrintPlanItem).columns}
    assert "plate_index" in cols
    uniques = {
        tuple(u.columns.keys())
        for u in ProjectPrintPlanItem.__table__.constraints
        if u.name == "uq_plan_project_file_plate"
    }
    assert ("project_id", "library_file_id", "plate_index") in uniques


@pytest.mark.asyncio
async def test_two_plates_of_one_file_can_coexist(db_session):
    project = Project(name="P")
    db_session.add(project)
    await db_session.flush()
    file = LibraryFile(filename="f.gcode.3mf", file_path="x", file_size=1, file_type="gcode")
    db_session.add(file)
    await db_session.flush()
    db_session.add(ProjectPrintPlanItem(project_id=project.id, library_file_id=file.id, copies=1, plate_index=1))
    db_session.add(ProjectPrintPlanItem(project_id=project.id, library_file_id=file.id, copies=2, plate_index=2))
    await db_session.commit()

    rows = (
        (await db_session.execute(select(ProjectPrintPlanItem).where(ProjectPrintPlanItem.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_seed_expands_a_multi_plate_row_and_leaves_single_plate_alone(db_session, test_engine):
    project = Project(name="P")
    db_session.add(project)
    await db_session.flush()
    multi = LibraryFile(
        filename="m.gcode.3mf",
        file_path="m",
        file_size=1,
        file_type="gcode",
        file_metadata={"plates": [{"index": 1}, {"index": 2}, {"index": 3}]},
    )
    single = LibraryFile(
        filename="s.gcode.3mf",
        file_path="s",
        file_size=1,
        file_type="gcode",
        file_metadata={"plates": [{"index": 1}]},
    )
    db_session.add_all([multi, single])
    await db_session.flush()
    db_session.add(ProjectPrintPlanItem(project_id=project.id, library_file_id=multi.id, copies=2, order_index=0))
    db_session.add(ProjectPrintPlanItem(project_id=project.id, library_file_id=single.id, copies=5, order_index=1))
    await db_session.commit()

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    await m158_parts_ledger.seed(maker)
    await m158_parts_ledger.seed(maker)  # idempotent — DEBUG re-runs the head migration

    rows = (
        (
            await db_session.execute(
                select(ProjectPrintPlanItem)
                .where(ProjectPrintPlanItem.project_id == project.id)
                .order_by(ProjectPrintPlanItem.library_file_id, ProjectPrintPlanItem.plate_index)
            )
        )
        .scalars()
        .all()
    )
    multi_rows = [r for r in rows if r.library_file_id == multi.id]
    single_rows = [r for r in rows if r.library_file_id == single.id]
    assert [r.plate_index for r in multi_rows] == [1, 2, 3]
    assert all(r.copies == 2 and r.order_index == 0 for r in multi_rows)
    assert len(single_rows) == 1 and single_rows[0].plate_index == 0 and single_rows[0].copies == 5


@pytest.mark.asyncio
async def test_seed_skips_a_corrupted_metadata_row_without_aborting(db_session, test_engine):
    """A junk ``plates`` value must not raise out of the loop and abort seed() —
    that would leave the migration unrecorded and permanently stuck retrying on
    every restart. The row is left untouched; a healthy row in the same run
    still expands."""
    project = Project(name="P")
    db_session.add(project)
    await db_session.flush()
    garbage = LibraryFile(
        filename="g.gcode.3mf",
        file_path="g",
        file_size=1,
        file_type="gcode",
        file_metadata={"plates": "not-a-list"},
    )
    healthy = LibraryFile(
        filename="h.gcode.3mf",
        file_path="h",
        file_size=1,
        file_type="gcode",
        file_metadata={"plates": [{"index": 1}, {"index": 2}]},
    )
    db_session.add_all([garbage, healthy])
    await db_session.flush()
    db_session.add(ProjectPrintPlanItem(project_id=project.id, library_file_id=garbage.id, copies=1, order_index=0))
    db_session.add(ProjectPrintPlanItem(project_id=project.id, library_file_id=healthy.id, copies=3, order_index=1))
    await db_session.commit()

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    await m158_parts_ledger.seed(maker)  # must not raise

    rows = (
        (
            await db_session.execute(
                select(ProjectPrintPlanItem)
                .where(ProjectPrintPlanItem.project_id == project.id)
                .order_by(ProjectPrintPlanItem.library_file_id, ProjectPrintPlanItem.plate_index)
            )
        )
        .scalars()
        .all()
    )
    garbage_rows = [r for r in rows if r.library_file_id == garbage.id]
    healthy_rows = [r for r in rows if r.library_file_id == healthy.id]
    assert len(garbage_rows) == 1 and garbage_rows[0].plate_index == 0, "corrupted row left untouched"
    assert [r.plate_index for r in healthy_rows] == [1, 2], "healthy multi-plate row in the same run still expands"


@pytest.mark.asyncio
async def test_seed_backfills_parts_for_an_archive_with_a_file(db_session, test_engine, tmp_path):
    f = tmp_path / "old.gcode.3mf"
    f.write_bytes(_3mf({1: "lid", 2: "lid"}))
    archive = PrintArchive(
        printer_id=1,
        filename="old.3mf",
        print_name="Old",
        file_path=str(f),
        file_size=1,
        status="completed",
        plate_index=1,
        defective_count=1,
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(archive)
    await db_session.commit()

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    await m158_parts_ledger.seed(maker)
    await m158_parts_ledger.seed(maker)  # idempotent

    rows = (
        (await db_session.execute(select(PrintArchivePart).where(PrintArchivePart.archive_id == archive.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1 and rows[0].quantity == 2
    assert rows[0].defective == 1, "mono-plate flat count attributed"


@pytest.mark.asyncio
async def test_seed_skips_archives_that_already_have_rows_and_missing_files(db_session, test_engine):
    seeded = PrintArchive(
        printer_id=1,
        filename="s.3mf",
        print_name="S",
        file_path="Z:/gone/s.3mf",
        file_size=1,
        status="completed",
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(seeded)
    await db_session.flush()
    db_session.add(
        PrintArchivePart(archive_id=seeded.id, name="x", name_key="x", identify_ids=[1], quantity=1, defective=0)
    )
    missing = PrintArchive(
        printer_id=1,
        filename="m.3mf",
        print_name="M",
        file_path="Z:/gone/m.3mf",
        file_size=1,
        status="completed",
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(missing)
    await db_session.commit()

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    await m158_parts_ledger.seed(maker)

    rows = (await db_session.execute(select(PrintArchivePart))).scalars().all()
    assert len(rows) == 1, "pre-seeded untouched, missing file skipped, nothing raises"
