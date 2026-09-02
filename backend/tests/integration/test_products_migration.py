"""m162: legacy projects → products + order lines, atomically, then drop.

The test engine builds TODAY's schema from the models, so the legacy tables
and columns are created here by hand (the exact shapes m016/m044/m158 left
behind), populated, and the migration's ``upgrade`` is run against them.
"""

import json

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.migrations import m162_products_and_orders as m162
from backend.app.migrations.helpers import get_table_columns, table_exists
from backend.app.models.archive import PrintArchive
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
    # bracket has an explicit target; lid has 0 (= not counted); clip is only known from plates.
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
        await m162.upgrade(conn)


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
    assert parts["lid.stl"].qty_per_unit == 0  # zero stays zero
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
async def test_legacy_tables_and_columns_are_gone_and_a_rerun_is_a_noop(db_session, test_engine, printer_factory):
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
async def test_fresh_install_has_nothing_to_convert(db_session, test_engine):
    await _run_upgrade(test_engine)  # no legacy tables at all
    assert (await db_session.execute(select(Product))).first() is None
