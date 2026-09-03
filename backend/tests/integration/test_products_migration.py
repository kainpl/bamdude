"""m158: legacy projects → products + order lines, atomically, then drop.

The test engine builds TODAY's schema from the models, so the legacy tables
and columns are created here by hand and populated, then the migration's
``upgrade`` is run against them.

Two starting shapes are exercised, because m158 has to survive both:

* **shape (i)** — straight from 0.5.5: ``project_print_plan_items`` has no
  ``plate_index`` and there is no ``project_parts`` table at all. The whole-file
  plan row is expanded per plate from the file's metadata during the conversion.
* **shape (ii)** — the DB of anyone who ran the unreleased parts-ledger form of
  m158 first: ``plate_index`` and ``project_parts`` are both there.

Shape (iii) — already converted — is the re-run, and must change nothing.
"""

import json
import logging

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.migrations import m158_products_and_orders as m158
from backend.app.migrations.helpers import get_table_columns, table_exists
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.product import Product, ProductPart, ProductPlate, product_files, product_folders
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine, ProjectProcurement

pytestmark = pytest.mark.integration

_LEGACY_DDL = [
    "ALTER TABLE projects ADD COLUMN target_count INTEGER",
    "ALTER TABLE projects ADD COLUMN target_parts_count INTEGER",
    "ALTER TABLE projects ADD COLUMN parent_id INTEGER",
    "ALTER TABLE projects ADD COLUMN is_template BOOLEAN DEFAULT 0",
    "ALTER TABLE projects ADD COLUMN template_source_id INTEGER",
    "ALTER TABLE projects ADD COLUMN budget FLOAT",
    "CREATE TABLE library_file_projects (file_id INTEGER NOT NULL, project_id INTEGER NOT NULL, PRIMARY KEY (file_id, project_id))",
    "CREATE TABLE library_folder_projects (folder_id INTEGER NOT NULL, project_id INTEGER NOT NULL, PRIMARY KEY (folder_id, project_id))",
    """CREATE TABLE project_print_plan_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL, library_file_id INTEGER NOT NULL,
        copies INTEGER NOT NULL DEFAULT 1, order_index INTEGER NOT NULL DEFAULT 0, plate_index INTEGER NOT NULL DEFAULT 0,
        created_at DATETIME, updated_at DATETIME)""",
    """CREATE TABLE project_parts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL, name VARCHAR(512) NOT NULL,
        name_key VARCHAR(512) NOT NULL, target_qty INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE project_bom_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL, name VARCHAR(255) NOT NULL,
        quantity_needed INTEGER DEFAULT 1, quantity_acquired INTEGER DEFAULT 0, unit_price FLOAT, sourcing_url VARCHAR(512),
        archive_id INTEGER, stl_filename VARCHAR(255), remarks TEXT, sort_order INTEGER DEFAULT 0,
        created_at DATETIME, updated_at DATETIME)""",
]

# Plate 1: two brackets + one lid. Plate 2: ten clips (cloned, hence _2.._10).
_MULTI_META = {
    "plates": [
        {
            "index": 1,
            "objects": ["bracket.stl", "lid.stl"],
            "printable_objects": {"1": "bracket.stl", "2": "bracket.stl_2", "3": "lid.stl"},
            "print_time_seconds": 3600,
            "filament_used_grams": 40.0,
            "filaments": [{"slot_id": 1, "type": "PETG", "color": "#000000", "used_grams": 40.0}],
        },
        {
            "index": 2,
            "objects": ["clip.stl"],
            "printable_objects": {str(i): ("clip.stl" if i == 1 else f"clip.stl_{i}") for i in range(1, 11)},
            "print_time_seconds": 1800,
            "filament_used_grams": 12.0,
            "filaments": [{"slot_id": 1, "type": "PETG", "color": "#000000", "used_grams": 12.0}],
        },
    ]
}


async def _legacy_fixture(db: AsyncSession, engine, printer_factory) -> dict:
    async with engine.begin() as conn:
        for ddl in _LEGACY_DDL:
            await conn.execute(text(ddl))

    # conftest's printer_factory knows the Printer constructor's required
    # columns; every test below takes it as a fixture and passes it in.
    printer = await printer_factory()
    db.add(PrinterQueue(printer_id=printer.id))
    folder = LibraryFolder(name="Voron")
    db.add(folder)
    await db.flush()
    multi = LibraryFile(
        filename="parts.gcode.3mf",
        file_path="parts",
        file_size=1,
        file_type="gcode",
        folder_id=folder.id,
        file_metadata=_MULTI_META,
    )
    db.add(multi)
    order = Project(name="Voron build", description="a printer", status="active", color="#ff0000")
    archived = Project(name="Old job", status="active")
    template = Project(name="Shelf template", status="active")
    db.add_all([order, archived, template])
    await db.flush()
    await db.execute(
        text(
            "UPDATE projects SET budget = 12.5, is_template = 0, target_count = 3, target_parts_count = 9 WHERE id = :id"
        ),
        {"id": order.id},
    )
    await db.execute(
        text("UPDATE projects SET status = 'archived', is_template = 0 WHERE id = :id"), {"id": archived.id}
    )
    await db.execute(
        text("UPDATE projects SET is_template = 1, parent_id = :p WHERE id = :id"), {"id": template.id, "p": order.id}
    )
    await db.execute(
        text("INSERT INTO library_file_projects (file_id, project_id) VALUES (:f, :p)"), {"f": multi.id, "p": order.id}
    )
    await db.execute(
        text("INSERT INTO library_folder_projects (folder_id, project_id) VALUES (:f, :p)"),
        {"f": folder.id, "p": order.id},
    )
    for plate, copies in ((1, 2), (2, 3)):
        await db.execute(
            text(
                "INSERT INTO project_print_plan_items (project_id, library_file_id, copies, order_index, plate_index) "
                "VALUES (:p, :f, :c, 0, :pl)"
            ),
            {"p": order.id, "f": multi.id, "c": copies, "pl": plate},
        )
    # bracket has an explicit target; clip is only known from plates. The two
    # zeros are the interesting pair: ``lid.stl`` IS on plate 1, so its zero is
    # the retired seed's default and must be derived away; ``ghost.stl`` is on
    # no plate at all, so nothing could have seeded it and its zero is an
    # operator's "don't measure this".
    await db.execute(
        text(
            "INSERT INTO project_parts (project_id, name, name_key, target_qty) VALUES (:p, 'bracket.stl', 'bracket.stl', 5)"
        ),
        {"p": order.id},
    )
    await db.execute(
        text("INSERT INTO project_parts (project_id, name, name_key, target_qty) VALUES (:p, 'lid.stl', 'lid.stl', 0)"),
        {"p": order.id},
    )
    await db.execute(
        text(
            "INSERT INTO project_parts (project_id, name, name_key, target_qty) VALUES (:p, 'ghost.stl', 'ghost.stl', 0)"
        ),
        {"p": order.id},
    )
    await db.execute(
        text(
            "INSERT INTO project_bom_items (project_id, name, quantity_needed, quantity_acquired, unit_price, sourcing_url, sort_order) "
            "VALUES (:p, 'M3 screw', 8, 3, 0.05, 'https://shop', 0)"
        ),
        {"p": order.id},
    )
    archive = PrintArchive(
        printer_id=printer.id,
        project_id=order.id,
        library_file_id=multi.id,
        plate_index=1,
        filename="parts.gcode.3mf",
        file_path="",
        file_size=0,
        status="completed",
        filament_type="PETG",
    )
    db.add(archive)
    await db.flush()
    queue = (await db.execute(select(PrinterQueue).where(PrinterQueue.printer_id == printer.id))).scalar_one()
    db.add(PrintQueueItem(queue_id=queue.id, library_file_id=multi.id, project_id=order.id, position=1))
    db.add(AutoQueueItem(library_file_id=multi.id, project_id=order.id, position=1))
    await db.commit()
    return {
        "order": order.id,
        "archived": archived.id,
        "template": template.id,
        "file": multi.id,
        "folder": folder.id,
        "archive": archive.id,
    }


async def _run_upgrade(engine):
    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA foreign_keys = OFF"))
        await m158.upgrade(conn)


@pytest.mark.asyncio
async def test_every_project_becomes_a_product_with_a_single_line(db_session, test_engine, printer_factory):
    ids = await _legacy_fixture(db_session, test_engine, printer_factory)
    await _run_upgrade(test_engine)
    db_session.expire_all()

    line = (await db_session.execute(select(ProjectLine).where(ProjectLine.project_id == ids["order"]))).scalar_one()
    assert line.quantity == 1 and line.material is None
    product = await db_session.get(Product, line.product_id)
    assert product.name == "Voron build" and product.description == "a printer" and product.is_active is True

    files = (
        (
            await db_session.execute(
                select(product_files.c.library_file_id).where(product_files.c.product_id == product.id)
            )
        )
        .scalars()
        .all()
    )
    folders = (
        (
            await db_session.execute(
                select(product_folders.c.library_folder_id).where(product_folders.c.product_id == product.id)
            )
        )
        .scalars()
        .all()
    )
    assert files == [ids["file"]] and folders == [ids["folder"]]

    plates = (
        (
            await db_session.execute(
                select(ProductPlate.plate_index)
                .where(ProductPlate.product_id == product.id)
                .order_by(ProductPlate.plate_index)
            )
        )
        .scalars()
        .all()
    )
    assert plates == [1, 2]

    parts = {
        p.name_key: p
        for p in (await db_session.execute(select(ProductPart).where(ProductPart.product_id == product.id))).scalars()
    }
    assert parts["bracket.stl"].qty_per_unit == 5 and parts["bracket.stl"].auto is False  # explicit target wins
    # lid: target 0, but it IS on plate 1 — the retired seed planted every
    # discovered part with 0, so that zero is derived away rather than kept
    # (2 copies of plate 1 × 1 lid). Keeping it would make the part unmeasurable.
    assert parts["lid.stl"].qty_per_unit == 2 and parts["lid.stl"].auto is True
    # ghost: target 0 and on no plate — nothing seeded it, so the zero is the
    # operator's "don't measure" and survives.
    assert parts["ghost.stl"].qty_per_unit == 0 and parts["ghost.stl"].auto is False
    # clip was never in project_parts: Σ copies × yield = 3 × 10, marked auto
    assert parts["clip.stl"].qty_per_unit == 30 and parts["clip.stl"].auto is True
    assert parts["clip.stl"].aliases == ["clip.stl"]
    screw = parts["purchased:m3 screw"]
    assert screw.kind == "purchased" and screw.qty_per_unit == 8 and screw.unit_price == 0.05
    proc = (
        await db_session.execute(select(ProjectProcurement).where(ProjectProcurement.project_id == ids["order"]))
    ).scalar_one()
    assert proc.product_part_id == screw.id and proc.quantity_acquired == 3

    archive = await db_session.get(PrintArchive, ids["archive"])
    assert archive.project_line_id == line.id
    queue_row = (
        await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.project_id == ids["order"]))
    ).scalar_one()
    assert queue_row.project_line_id == line.id
    auto_row = (
        await db_session.execute(select(AutoQueueItem).where(AutoQueueItem.project_id == ids["order"]))
    ).scalar_one()
    assert auto_row.project_line_id == line.id

    project = await db_session.get(Project, ids["order"])
    assert project.price == 12.5


@pytest.mark.asyncio
async def test_archived_becomes_completed_and_templates_become_products_only(db_session, test_engine, printer_factory):
    ids = await _legacy_fixture(db_session, test_engine, printer_factory)
    await _run_upgrade(test_engine)
    db_session.expire_all()

    assert (await db_session.get(Project, ids["archived"])).status == "completed"
    assert await db_session.get(Project, ids["template"]) is None
    names = (await db_session.execute(select(Product.name))).scalars().all()
    assert "Shelf template" in names
    assert (
        await db_session.execute(select(ProjectLine).where(ProjectLine.project_id == ids["template"]))
    ).first() is None


@pytest.mark.asyncio
async def test_shape_iii_already_converted_is_a_noop(db_session, test_engine, printer_factory):
    await _legacy_fixture(db_session, test_engine, printer_factory)
    await _run_upgrade(test_engine)
    await _run_upgrade(test_engine)  # DEBUG=true re-runs the head migration
    async with test_engine.begin() as conn:
        for table in (
            "project_bom_items",
            "project_print_plan_items",
            "project_parts",
            "library_file_projects",
            "library_folder_projects",
        ):
            assert not await table_exists(conn, table)
        cols = set(await get_table_columns(conn, "projects"))
        assert not cols & {
            "target_count",
            "target_parts_count",
            "parent_id",
            "is_template",
            "template_source_id",
            "budget",
        }
        assert {"customer_id", "price"} <= cols
    db_session.expire_all()
    assert len((await db_session.execute(select(Product))).scalars().all()) == 3  # not 6


@pytest.mark.asyncio
async def test_one_corrupt_file_metadata_does_not_abort_the_upgrade(db_session, test_engine, printer_factory, caplog):
    ids = await _legacy_fixture(db_session, test_engine, printer_factory)
    # Two shapes of junk, and only one of them reaches the try/except.
    # ``_load_meta`` already swallows JSON *syntax* errors, and
    # ``{"plates": <not a list>}`` is tolerated by ``_plate_names`` — iterating
    # it simply yields no names. A top-level non-object is what actually makes
    # ``meta.get()`` raise, so both shapes are pinned here.
    tolerated = LibraryFile(
        filename="tolerated.gcode.3mf",
        file_path="tolerated",
        file_size=1,
        file_type="gcode",
        folder_id=ids["folder"],
        file_metadata={"plates": "not-a-list"},
    )
    raising = LibraryFile(
        filename="raising.gcode.3mf",
        file_path="raising",
        file_size=1,
        file_type="gcode",
        folder_id=ids["folder"],
        file_metadata=["not", "a", "dict"],
    )
    db_session.add_all([tolerated, raising])
    await db_session.flush()
    tolerated_id, raising_id = tolerated.id, raising.id
    for file_id in (tolerated_id, raising_id):
        await db_session.execute(
            text(
                "INSERT INTO project_print_plan_items (project_id, library_file_id, copies, order_index, plate_index) "
                "VALUES (:p, :f, 1, 1, 1)"
            ),
            {"p": ids["order"], "f": file_id},
        )
    await db_session.commit()

    with caplog.at_level(logging.WARNING, logger="backend.app.migrations.m158_products_and_orders"):
        await _run_upgrade(test_engine)  # must not raise
    assert "skipped metadata" in caplog.text  # the guard actually fired, not "nothing raised"
    db_session.expire_all()

    line = (await db_session.execute(select(ProjectLine).where(ProjectLine.project_id == ids["order"]))).scalar_one()
    parts = {
        p.name_key
        for p in (
            await db_session.execute(select(ProductPart).where(ProductPart.product_id == line.product_id))
        ).scalars()
    }
    assert {"bracket.stl", "lid.stl", "clip.stl"} <= parts  # the healthy file converted anyway
    plate_files = (
        (
            await db_session.execute(
                select(ProductPlate.library_file_id).where(ProductPlate.product_id == line.product_id)
            )
        )
        .scalars()
        .all()
    )
    assert tolerated_id in plate_files and raising_id in plate_files  # both junk files still got their plate row


@pytest.mark.asyncio
async def test_fresh_install_has_nothing_to_convert(db_session, test_engine):
    await _run_upgrade(test_engine)  # no legacy tables at all
    assert (await db_session.execute(select(Product))).first() is None


# ---------------------------------------------------------------------------
# Shape (i): straight from 0.5.5 — no plate_index, no project_parts
# ---------------------------------------------------------------------------

_LEGACY_DDL_0_5_5 = [
    "ALTER TABLE projects ADD COLUMN target_count INTEGER",
    "ALTER TABLE projects ADD COLUMN target_parts_count INTEGER",
    "ALTER TABLE projects ADD COLUMN parent_id INTEGER",
    "ALTER TABLE projects ADD COLUMN is_template BOOLEAN DEFAULT 0",
    "ALTER TABLE projects ADD COLUMN template_source_id INTEGER",
    "ALTER TABLE projects ADD COLUMN budget FLOAT",
    "CREATE TABLE library_file_projects (file_id INTEGER NOT NULL, project_id INTEGER NOT NULL, PRIMARY KEY (file_id, project_id))",
    "CREATE TABLE library_folder_projects (folder_id INTEGER NOT NULL, project_id INTEGER NOT NULL, PRIMARY KEY (folder_id, project_id))",
    # No plate_index — a plan row still means "N × the whole file".
    """CREATE TABLE project_print_plan_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL, library_file_id INTEGER NOT NULL,
        copies INTEGER NOT NULL DEFAULT 1, order_index INTEGER NOT NULL DEFAULT 0,
        created_at DATETIME, updated_at DATETIME)""",
    # No project_parts at all — that table only ever existed in the unreleased
    # parts-ledger form of m158.
    """CREATE TABLE project_bom_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL, name VARCHAR(255) NOT NULL,
        quantity_needed INTEGER DEFAULT 1, quantity_acquired INTEGER DEFAULT 0, unit_price FLOAT, sourcing_url VARCHAR(512),
        archive_id INTEGER, stl_filename VARCHAR(255), remarks TEXT, sort_order INTEGER DEFAULT 0,
        created_at DATETIME, updated_at DATETIME)""",
]

# One plate, one object: nothing to expand, so the row stays plate_index = 0.
_SINGLE_META = {
    "plates": [
        {
            "index": 1,
            "objects": ["knob.stl"],
            "printable_objects": {"1": "knob.stl"},
            "print_time_seconds": 600,
            "filament_used_grams": 5.0,
        }
    ]
}


async def _fixture_0_5_5(db: AsyncSession, engine, printer_factory) -> dict:
    async with engine.begin() as conn:
        for ddl in _LEGACY_DDL_0_5_5:
            await conn.execute(text(ddl))

    printer = await printer_factory()
    db.add(PrinterQueue(printer_id=printer.id))
    folder = LibraryFolder(name="Voron")
    db.add(folder)
    await db.flush()
    multi = LibraryFile(
        filename="parts.gcode.3mf",
        file_path="parts",
        file_size=1,
        file_type="gcode",
        folder_id=folder.id,
        file_metadata=_MULTI_META,
    )
    single = LibraryFile(
        filename="knob.gcode.3mf",
        file_path="knob",
        file_size=1,
        file_type="gcode",
        folder_id=folder.id,
        file_metadata=_SINGLE_META,
    )
    db.add_all([multi, single])
    order = Project(name="Voron build", status="active")
    db.add(order)
    await db.flush()
    await db.execute(text("UPDATE projects SET is_template = 0 WHERE id = :id"), {"id": order.id})
    await db.execute(
        text("INSERT INTO library_file_projects (file_id, project_id) VALUES (:f, :p)"), {"f": multi.id, "p": order.id}
    )
    # One row per file, each meaning "N × the whole file".
    for file_id, copies in ((multi.id, 3), (single.id, 5)):
        await db.execute(
            text(
                "INSERT INTO project_print_plan_items (project_id, library_file_id, copies, order_index) "
                "VALUES (:p, :f, :c, 0)"
            ),
            {"p": order.id, "f": file_id, "c": copies},
        )
    archive = PrintArchive(
        printer_id=printer.id,
        project_id=order.id,
        library_file_id=multi.id,
        plate_index=1,
        filename="parts.gcode.3mf",
        file_path="",
        file_size=0,
        status="completed",
        filament_type="PETG",
    )
    db.add(archive)
    await db.flush()
    await db.commit()
    return {"order": order.id, "multi": multi.id, "single": single.id, "archive": archive.id}


@pytest.mark.asyncio
async def test_shape_i_straight_from_0_5_5_expands_the_plan_from_metadata(db_session, test_engine, printer_factory):
    """No ``plate_index`` column: the whole-file plan row becomes one plate row
    per plate of the file, each inheriting ``copies``, so Σ copies × yield is
    the same number the old per-plate expansion seed would have produced."""
    ids = await _fixture_0_5_5(db_session, test_engine, printer_factory)
    await _run_upgrade(test_engine)
    db_session.expire_all()

    line = (await db_session.execute(select(ProjectLine).where(ProjectLine.project_id == ids["order"]))).scalar_one()
    assert line.quantity == 1
    product_id = line.product_id

    plates = sorted(
        (
            await db_session.execute(
                select(ProductPlate.library_file_id, ProductPlate.plate_index).where(
                    ProductPlate.product_id == product_id
                )
            )
        ).all()
    )
    assert plates == sorted([(ids["multi"], 1), (ids["multi"], 2), (ids["single"], 0)])

    parts = {
        p.name_key: p
        for p in (await db_session.execute(select(ProductPart).where(ProductPart.product_id == product_id))).scalars()
    }
    assert parts["bracket.stl"].qty_per_unit == 6  # 3 copies × 2 on plate 1
    assert parts["lid.stl"].qty_per_unit == 3  # 3 copies × 1 on plate 1
    assert parts["clip.stl"].qty_per_unit == 30  # 3 copies × 10 on plate 2
    assert parts["knob.stl"].qty_per_unit == 5  # single-plate file, 5 copies of the whole file
    assert all(p.auto is True for p in parts.values())  # no project_parts table = no explicit targets

    archive = await db_session.get(PrintArchive, ids["archive"])
    assert archive.project_line_id == line.id


@pytest.mark.asyncio
async def test_print_archive_parts_table_is_created_on_shape_i(db_session, test_engine, printer_factory):
    """The parts-ledger DDL of the old m158 has to survive the fold — ``seed()``
    backfills into it, and a 0.5.5 install has never seen it."""
    await _fixture_0_5_5(db_session, test_engine, printer_factory)
    async with test_engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS print_archive_parts"))
        assert not await table_exists(conn, "print_archive_parts")
    await _run_upgrade(test_engine)
    async with test_engine.begin() as conn:
        assert await table_exists(conn, "print_archive_parts")


@pytest.mark.asyncio
async def test_shape_i_unreadable_metadata_keeps_the_row_as_a_whole_file_plate(
    db_session, test_engine, printer_factory, caplog
):
    """Expansion is where a 0.5.5 plan row meets its file's metadata, so it is
    also where junk shows up — and the only place it can be reported: the same
    bytes reach ``_plate_key_counts`` as an empty dict, which raises nothing."""
    ids = await _fixture_0_5_5(db_session, test_engine, printer_factory)
    raising = LibraryFile(
        filename="raising.gcode.3mf",
        file_path="raising",
        file_size=1,
        file_type="gcode",
        folder_id=(await db_session.get(LibraryFile, ids["multi"])).folder_id,
        file_metadata=["not", "a", "dict"],
    )
    db_session.add(raising)
    await db_session.flush()
    raising_id = raising.id
    await db_session.execute(
        text(
            "INSERT INTO project_print_plan_items (project_id, library_file_id, copies, order_index) "
            "VALUES (:p, :f, 2, 1)"
        ),
        {"p": ids["order"], "f": raising_id},
    )
    await db_session.commit()

    with caplog.at_level(logging.WARNING, logger="backend.app.migrations.m158_products_and_orders"):
        await _run_upgrade(test_engine)  # must not raise
    assert "unreadable metadata" in caplog.text  # the guard fired, not "nothing raised"
    db_session.expire_all()

    line = (await db_session.execute(select(ProjectLine).where(ProjectLine.project_id == ids["order"]))).scalar_one()
    plates = {
        (f, p)
        for f, p in (
            await db_session.execute(
                select(ProductPlate.library_file_id, ProductPlate.plate_index).where(
                    ProductPlate.product_id == line.product_id
                )
            )
        ).all()
    }
    assert (raising_id, 0) in plates  # kept as one whole-file plate, not expanded away
    assert (ids["multi"], 1) in plates and (ids["multi"], 2) in plates  # the healthy file expanded anyway
