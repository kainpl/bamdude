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

import io
import json
import logging
import zipfile

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
from backend.app.services.library_objects_backfill import backfill_library_objects

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


# ---------------------------------------------------------------------------
# Rules A, B and D — the legacy targets survive the conversion (2026-09-03)
# ---------------------------------------------------------------------------

# Plate 1: three bodies. Plate 2: six lids. With ``copies = 2`` on each plan
# row the derived yield is body 6 / lid 12 — deliberately unrelated to the
# targets below, so a test that passes can only be reading the targets.
_KIT_META = {
    "plates": [
        {
            "index": 1,
            "objects": ["body.stl"],
            "printable_objects": {"1": "body.stl", "2": "body.stl_2", "3": "body.stl_3"},
        },
        {
            "index": 2,
            "objects": ["lid.stl"],
            "printable_objects": {str(i): ("lid.stl" if i == 1 else f"lid.stl_{i}") for i in range(1, 7)},
        },
    ]
}

# One plate, 3 × a + 12 × b. Shape (i) reads it as a single whole-file plate.
_THIRTY_META = {
    "plates": [
        {
            "index": 1,
            "objects": ["a.stl", "b.stl"],
            "printable_objects": (
                {str(i): ("a.stl" if i == 1 else f"a.stl_{i}") for i in range(1, 4)}
                | {str(i + 3): ("b.stl" if i == 1 else f"b.stl_{i}") for i in range(1, 13)}
            ),
        }
    ]
}


async def _kit_fixture(
    db: AsyncSession, engine, target_sets: list[dict[str, int]], boms: list[list[tuple[str, int, int]]] | None = None
) -> list[int]:
    """Shape (ii), one project per entry: plate yield body 6 / lid 12, own targets.

    ``boms[i]`` is that project's ``(name, quantity_needed, quantity_acquired)``
    rows — the legacy PROJECT totals, which is the whole point of the pairing."""
    async with engine.begin() as conn:
        for ddl in _LEGACY_DDL:
            await conn.execute(text(ddl))

    folder = LibraryFolder(name="Kits")
    db.add(folder)
    await db.flush()
    kit = LibraryFile(
        filename="kit.gcode.3mf",
        file_path="kit",
        file_size=1,
        file_type="gcode",
        folder_id=folder.id,
        file_metadata=_KIT_META,
    )
    db.add(kit)
    await db.flush()

    project_ids: list[int] = []
    for i, targets in enumerate(target_sets):
        project = Project(name=f"Kit {i}", status="active")
        db.add(project)
        await db.flush()
        await db.execute(text("UPDATE projects SET is_template = 0 WHERE id = :id"), {"id": project.id})
        for plate in (1, 2):
            await db.execute(
                text(
                    "INSERT INTO project_print_plan_items (project_id, library_file_id, copies, order_index, "
                    "plate_index) VALUES (:p, :f, 2, 0, :pl)"
                ),
                {"p": project.id, "f": kit.id, "pl": plate},
            )
        for key, target in targets.items():
            await db.execute(
                text("INSERT INTO project_parts (project_id, name, name_key, target_qty) VALUES (:p, :n, :k, :t)"),
                {"p": project.id, "n": key, "k": key, "t": target},
            )
        for bom_name, needed, acquired in (boms or [[]] * len(target_sets))[i]:
            await db.execute(
                text(
                    "INSERT INTO project_bom_items (project_id, name, quantity_needed, quantity_acquired, "
                    "sort_order) VALUES (:p, :n, :needed, :acquired, 0)"
                ),
                {"p": project.id, "n": bom_name, "needed": needed, "acquired": acquired},
            )
        project_ids.append(project.id)
    await db.commit()
    return project_ids


async def _parts_of(db: AsyncSession, product_id: int) -> dict:
    return {
        p.name_key: p
        for p in (await db.execute(select(ProductPart).where(ProductPart.product_id == product_id))).scalars()
    }


async def _line_of(db: AsyncSession, project_id: int) -> ProjectLine:
    return (await db.execute(select(ProjectLine).where(ProjectLine.project_id == project_id))).scalar_one()


@pytest.mark.asyncio
async def test_rule_a_part_targets_become_a_kit_times_n(db_session, test_engine):
    """Every counted part has a target → the product reads as a kit × N.

    ``N = gcd(targets)``, ``qty_per_unit = target / N``, so the need per part
    (``N × qty_per_unit``) is exactly the old target. Coprime targets give
    ``N = 1``, i.e. the original rule, unchanged."""
    ids = await _kit_fixture(
        db_session,
        test_engine,
        [
            {"body.stl": 780, "lid.stl": 780},
            {"body.stl": 236, "lid.stl": 118},
            {"body.stl": 7, "lid.stl": 5},
        ],
    )
    await _run_upgrade(test_engine)
    db_session.expire_all()

    line = await _line_of(db_session, ids[0])
    parts = await _parts_of(db_session, line.product_id)
    assert line.quantity == 780
    assert parts["body.stl"].qty_per_unit == 1 and parts["body.stl"].auto is False
    assert parts["lid.stl"].qty_per_unit == 1 and parts["lid.stl"].auto is False

    line = await _line_of(db_session, ids[1])
    parts = await _parts_of(db_session, line.product_id)
    assert line.quantity == 118
    assert parts["body.stl"].qty_per_unit == 2 and parts["lid.stl"].qty_per_unit == 1

    line = await _line_of(db_session, ids[2])
    parts = await _parts_of(db_session, line.product_id)
    assert line.quantity == 1  # coprime: nothing to factor out
    assert parts["body.stl"].qty_per_unit == 7 and parts["lid.stl"].qty_per_unit == 5


@pytest.mark.asyncio
async def test_rule_a_leaves_an_operator_zero_and_a_derived_part_alone(db_session, test_engine):
    """Rule A needs EVERY counted part to carry a target.

    ``lid.stl`` is on a plate and has no target of its own, so a kit split
    would be a guess about a part nobody sized: rule A stands down, the
    targets stay as they are and rule B decides the quantity. The operator
    zero (a part on no plate) is untouched either way."""
    (project_id,) = await _kit_fixture(db_session, test_engine, [{"body.stl": 780, "ghost.stl": 0}])
    await _run_upgrade(test_engine)
    db_session.expire_all()

    line = await _line_of(db_session, project_id)
    parts = await _parts_of(db_session, line.product_id)
    assert parts["body.stl"].qty_per_unit == 780 and parts["body.stl"].auto is False  # NOT divided by 780
    assert parts["ghost.stl"].qty_per_unit == 0 and parts["ghost.stl"].auto is False
    assert parts["lid.stl"].qty_per_unit == 12 and parts["lid.stl"].auto is True  # 2 copies × 6
    assert line.quantity == 1  # no project-level target to divide either


async def _plan_target_fixture(
    db: AsyncSession, engine, cases: list[tuple[int, int, int]], meta: dict | None = None
) -> list[int]:
    """Shape (i), one project per ``(copies, target_parts_count, target_count)``."""
    async with engine.begin() as conn:
        for ddl in _LEGACY_DDL_0_5_5:
            await conn.execute(text(ddl))

    folder = LibraryFolder(name="Runs")
    db.add(folder)
    await db.flush()
    plate = LibraryFile(
        filename="run.gcode.3mf",
        file_path="run",
        file_size=1,
        file_type="gcode",
        folder_id=folder.id,
        file_metadata=meta or _THIRTY_META,
    )
    db.add(plate)
    await db.flush()

    project_ids: list[int] = []
    for i, (copies, target_parts_count, target_count) in enumerate(cases):
        project = Project(name=f"Run {i}", status="active")
        db.add(project)
        await db.flush()
        await db.execute(
            text("UPDATE projects SET is_template = 0, target_parts_count = :tp, target_count = :tc WHERE id = :id"),
            {"id": project.id, "tp": target_parts_count, "tc": target_count},
        )
        await db.execute(
            text(
                "INSERT INTO project_print_plan_items (project_id, library_file_id, copies, order_index) "
                "VALUES (:p, :f, :c, 0)"
            ),
            {"p": project.id, "f": plate.id, "c": copies},
        )
        project_ids.append(project.id)
    await db.commit()
    return project_ids


@pytest.mark.asyncio
async def test_rule_b_target_parts_count_sets_the_quantity_when_it_divides(db_session, test_engine):
    """No part targets, but the project counted parts: 1560 / 30 per run = 52.

    A remainder means the two numbers never described the same plan, so the
    quantity falls back to 1 rather than rounding the operator's target."""
    ids = await _plan_target_fixture(db_session, test_engine, [(2, 1560, 0), (2, 1561, 0)])
    await _run_upgrade(test_engine)
    db_session.expire_all()

    line = await _line_of(db_session, ids[0])
    parts = await _parts_of(db_session, line.product_id)
    assert line.quantity == 52
    assert parts["a.stl"].qty_per_unit == 6 and parts["a.stl"].auto is True
    assert parts["b.stl"].qty_per_unit == 24 and parts["b.stl"].auto is True

    assert (await _line_of(db_session, ids[1])).quantity == 1  # 1561 % 30 != 0


@pytest.mark.asyncio
async def test_rule_b_target_count_over_plan_copies(db_session, test_engine):
    """``target_count`` is the second chance: whole plan runs, not parts."""
    ids = await _plan_target_fixture(db_session, test_engine, [(26, 0, 26), (26, 0, 52), (26, 0, 27)])
    await _run_upgrade(test_engine)
    db_session.expire_all()

    assert (await _line_of(db_session, ids[0])).quantity == 1
    assert (await _line_of(db_session, ids[1])).quantity == 2
    assert (await _line_of(db_session, ids[2])).quantity == 1  # 27 % 26 != 0


async def _history_only_fixture(
    db: AsyncSession, engine, printer_factory, archives: list[tuple], bom: list[tuple[str, int, int]] | None = None
) -> dict:
    """A project whose plan file was deleted years ago — only archives remain."""
    async with engine.begin() as conn:
        for ddl in _LEGACY_DDL_0_5_5:
            await conn.execute(text(ddl))

    printer = await printer_factory()
    order = Project(name="Order 42", status="active")
    db.add(order)
    await db.flush()
    await db.execute(text("UPDATE projects SET is_template = 0 WHERE id = :id"), {"id": order.id})
    # The plan still names a library file that no longer exists: the
    # conversion's JOIN drops the row, so the product gets no plates, no
    # yields and therefore no printed parts at all.
    await db.execute(
        text(
            "INSERT INTO project_print_plan_items (project_id, library_file_id, copies, order_index) "
            "VALUES (:p, 999999, 4, 0)"
        ),
        {"p": order.id},
    )
    for bom_name, needed, acquired in bom or []:
        await db.execute(
            text(
                "INSERT INTO project_bom_items (project_id, name, quantity_needed, quantity_acquired, sort_order) "
                "VALUES (:p, :n, :needed, :acquired, 0)"
            ),
            {"p": order.id, "n": bom_name, "needed": needed, "acquired": acquired},
        )
    for status, parts in archives:
        archive = PrintArchive(
            printer_id=printer.id,
            project_id=order.id,
            plate_index=1,
            filename="gone.gcode.3mf",
            file_path="",
            file_size=0,
            status=status,
            filament_type="PETG",
        )
        db.add(archive)
        await db.flush()
        for part_name, quantity, defective in parts:
            await db.execute(
                text(
                    "INSERT INTO print_archive_parts (archive_id, name, name_key, quantity, defective) "
                    "VALUES (:a, :n, :k, :q, :d)"
                ),
                {"a": archive.id, "n": part_name, "k": part_name.lower(), "q": quantity, "d": defective},
            )
    await db.commit()
    return {"order": order.id}


async def _run_seed(engine):
    await m158.seed(async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False))


@pytest.mark.asyncio
async def test_rule_d_history_only_product_gets_parts_from_its_archives(db_session, test_engine, printer_factory):
    """Nothing on disk, hundreds of archives: the parts come from the history.

    ``qty_per_unit`` is the gcd-normalised share and the line quantity is the
    completed usable total — which is the old target whenever the prints were
    actually run to it. An unfinished print is not history yet."""
    ids = await _history_only_fixture(
        db_session,
        test_engine,
        printer_factory,
        [
            ("completed", [("hfb.stl", 2, 0)]),
            ("completed", [("hfb.stl", 2, 0)]),
            ("completed", [("hfb.stl", 3, 1)]),
            ("printing", [("hfb.stl", 4, 0)]),
        ],
    )
    await _run_upgrade(test_engine)
    await _run_seed(test_engine)
    db_session.expire_all()

    line = await _line_of(db_session, ids["order"])
    parts = await _parts_of(db_session, line.product_id)
    assert set(parts) == {"hfb.stl"}
    assert parts["hfb.stl"].qty_per_unit == 1 and parts["hfb.stl"].auto is False
    assert parts["hfb.stl"].aliases == ["hfb.stl"]
    assert line.quantity == 6  # 2 + 2 + (3 - 1); the printing archive is not counted

    await _run_seed(test_engine)  # idempotent — the product now has printed parts
    db_session.expire_all()
    line = await _line_of(db_session, ids["order"])
    assert len(await _parts_of(db_session, line.product_id)) == 1 and line.quantity == 6


@pytest.mark.asyncio
async def test_rule_d_splits_several_keys_by_gcd(db_session, test_engine, printer_factory):
    ids = await _history_only_fixture(
        db_session,
        test_engine,
        printer_factory,
        [
            ("completed", [("body.stl", 12, 0), ("lid.stl", 6, 0)]),
            ("completed", [("body.stl", 8, 0), ("lid.stl", 5, 1)]),
        ],
    )
    await _run_upgrade(test_engine)
    await _run_seed(test_engine)
    db_session.expire_all()

    line = await _line_of(db_session, ids["order"])
    parts = await _parts_of(db_session, line.product_id)
    assert parts["body.stl"].qty_per_unit == 2 and parts["lid.stl"].qty_per_unit == 1  # 20 : 10
    assert line.quantity == 10  # 30 usable / 3 per unit


@pytest.mark.asyncio
async def test_rule_d_never_overwrites_an_edited_quantity(db_session, test_engine, printer_factory):
    """The parts are missing information; the quantity may already be a decision."""
    ids = await _history_only_fixture(
        db_session, test_engine, printer_factory, [("completed", [("hfb.stl", 6, 0)])], bom=[("Glue", 6, 6)]
    )
    await _run_upgrade(test_engine)
    async with test_engine.begin() as conn:
        await conn.execute(text("UPDATE project_lines SET quantity = 5 WHERE project_id = :p"), {"p": ids["order"]})
    await _run_seed(test_engine)
    db_session.expire_all()

    line = await _line_of(db_session, ids["order"])
    parts = await _parts_of(db_session, line.product_id)
    assert parts["hfb.stl"].qty_per_unit == 1
    assert line.quantity == 5
    # The quantity did not move, so nothing may be divided by it either: the
    # purchased total is only ever rescaled together with the raise that would
    # otherwise have multiplied it.
    assert parts["purchased:glue"].qty_per_unit == 6


@pytest.mark.asyncio
async def test_purchased_totals_divide_by_the_kit_count(db_session, test_engine):
    """A BOM row counted the whole PROJECT, and the line now multiplies it.

    Leaving the total in ``qty_per_unit`` was only ever right while the line
    was the literal × 1: with rule A's × 780 the order would ask for 608 400
    screws against the 780 recorded as acquired, and ``_units_complete``
    (acquired // qty_per_unit) would cap a fully printed order at one unit
    forever. What was bought stays an absolute total — that side is compared
    against ``qty_per_unit × quantity`` and is correct as it is."""
    (project_id,) = await _kit_fixture(
        db_session,
        test_engine,
        [{"body.stl": 780, "lid.stl": 780}],
        boms=[[("M3 screw", 780, 780), ("Bearing pack", 1000, 400)]],
    )
    await _run_upgrade(test_engine)
    db_session.expire_all()

    line = await _line_of(db_session, project_id)
    parts = await _parts_of(db_session, line.product_id)
    assert line.quantity == 780
    assert parts["purchased:m3 screw"].qty_per_unit == 1  # 780 / 780
    # 1000 for 780 kits is not a whole number of units: round UP, because
    # under-provisioning a requirement is the direction that stops a build.
    assert parts["purchased:bearing pack"].qty_per_unit == 2
    acquired = {
        row.product_part_id: row.quantity_acquired
        for row in (
            await db_session.execute(select(ProjectProcurement).where(ProjectProcurement.project_id == project_id))
        ).scalars()
    }
    assert acquired[parts["purchased:m3 screw"].id] == 780  # absolute, untouched
    assert acquired[parts["purchased:bearing pack"].id] == 400


@pytest.mark.asyncio
async def test_rule_d_divides_purchased_totals_when_it_raises_the_quantity(db_session, test_engine, printer_factory):
    """Rule D raises the line the same way, so it owes the same division."""
    ids = await _history_only_fixture(
        db_session,
        test_engine,
        printer_factory,
        [
            ("completed", [("hfb.stl", 2, 0)]),
            ("completed", [("hfb.stl", 2, 0)]),
            ("completed", [("hfb.stl", 3, 1)]),
        ],
        bom=[("Glue", 6, 6)],
    )
    await _run_upgrade(test_engine)
    db_session.expire_all()
    line = await _line_of(db_session, ids["order"])
    # Converted at × 1, so the total went in untouched — rule D is what moves it.
    assert line.quantity == 1 and (await _parts_of(db_session, line.product_id))["purchased:glue"].qty_per_unit == 6

    await _run_seed(test_engine)
    db_session.expire_all()
    line = await _line_of(db_session, ids["order"])
    parts = await _parts_of(db_session, line.product_id)
    assert line.quantity == 6
    assert parts["purchased:glue"].qty_per_unit == 1
    proc = (
        await db_session.execute(select(ProjectProcurement).where(ProjectProcurement.project_id == ids["order"]))
    ).scalar_one()
    assert proc.quantity_acquired == 6  # absolute, untouched

    await _run_seed(test_engine)  # idempotent — no second division
    db_session.expire_all()
    line = await _line_of(db_session, ids["order"])
    assert line.quantity == 6
    assert (await _parts_of(db_session, line.product_id))["purchased:glue"].qty_per_unit == 1


@pytest.mark.asyncio
async def test_rule_b_counts_the_raw_plan_copies_not_the_expanded_plates(db_session, test_engine):
    """``target_count`` counts plan RUNS, so its divisor is the plan's own Σ copies.

    On shape (i) a plan row is expanded into one row per plate of the file,
    each inheriting ``copies`` — summing THAT would multiply the divisor by the
    plate count. Here one row of ``copies = 2`` on a two-plate file: the raw
    sum is 2, the expanded sum would be 4."""
    ids = await _plan_target_fixture(db_session, test_engine, [(2, 0, 2), (2, 0, 4)], meta=_MULTI_META)
    await _run_upgrade(test_engine)
    db_session.expire_all()

    assert (await _line_of(db_session, ids[0])).quantity == 1  # 2 / 2
    # The discriminating one: raw 4 / 2 = 2, where the expanded sum would give
    # 4 / 4 = 1 and quietly halve every requirement on the order.
    assert (await _line_of(db_session, ids[1])).quantity == 2


# ---------------------------------------------------------------------------
# The library half of the same hole (spec §G): a FILE that never knew its
# objects. The archive backfill above cannot help it — its product has no
# plates, no parts, and a plan with nothing to count.
# ---------------------------------------------------------------------------


def _sliced_3mf(objects_by_plate: dict[int, list[str]]) -> bytes:
    identify = 100
    plates = []
    for index, names in sorted(objects_by_plate.items()):
        objects = ""
        for name in names:
            identify += 1
            objects += f'<object identify_id="{identify}" name="{name}" skipped="false" />'
        plates.append(f'<plate><metadata key="index" value="{index}" />{objects}</plate>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("3D/3dmodel.model", '<?xml version="1.0"?><model/>')
        zf.writestr("Metadata/slice_info.config", '<?xml version="1.0"?><config>' + "".join(plates) + "</config>")
        for index in sorted(objects_by_plate):
            zf.writestr(f"Metadata/plate_{index}.gcode", b"; sliced\n")
    return buf.getvalue()


async def _seed_watching_for_orm(engine, monkeypatch) -> list[int]:
    """``seed()`` with ``sync_product_for_file`` spied on — it must never fire.

    ⚠️ Not style. That function emits ``select(ProductPlate)`` and
    ``select(ProductPart)`` — entity-wide selects built from TODAY's models. A
    migration runs mid-chain, so the day a later migration adds a column to
    either table this seed would emit SQL the database does not have yet, for
    every user upgrading from an older release. The seed derives its parts in
    named-column text SQL instead.
    """
    from backend.app.services import library_objects_backfill as _backfill, product_sync as _sync

    calls: list[int] = []

    async def spy(db, *, library_file_id, product_ids):
        calls.append(library_file_id)

    monkeypatch.setattr(_backfill, "sync_product_for_file", spy)
    monkeypatch.setattr(_sync, "sync_product_for_file", spy)
    await _run_seed(engine)
    return calls


@pytest.mark.asyncio
async def test_a_converted_order_gets_parts_from_a_file_that_never_knew_its_objects(
    db_session, test_engine, tmp_path, monkeypatch
):
    """⚠️ What running the library backfill FIRST in ``seed()`` buys.

    The conversion reads ``file_metadata`` to derive a product's printed parts. A
    3MF that never got its ``printable_objects`` gives it nothing, so the order
    converts to a product with a plate and no parts — nothing to measure, no
    progress, a plan that counts nothing. The seed fills the objects and then
    derives the parts from them, before rule D can claim the product as
    history-only.
    """
    async with test_engine.begin() as conn:
        for ddl in _LEGACY_DDL:
            await conn.execute(text(ddl))

    target = tmp_path / "bracket.gcode.3mf"
    target.write_bytes(_sliced_3mf({1: ["bracket.stl", "bracket.stl", "lid.stl"]}))
    file = LibraryFile(
        filename="bracket.gcode.3mf",
        file_path=str(target),
        file_size=1,
        file_type="gcode",
        file_metadata=None,  # never parsed — the whole premise
    )
    project = Project(name="Bracket order", status="active")
    db_session.add_all([file, project])
    await db_session.flush()
    await db_session.execute(text("UPDATE projects SET is_template = 0 WHERE id = :id"), {"id": project.id})
    await db_session.execute(
        text("INSERT INTO library_file_projects (file_id, project_id) VALUES (:f, :p)"),
        {"f": file.id, "p": project.id},
    )
    await db_session.execute(
        text(
            "INSERT INTO project_print_plan_items (project_id, library_file_id, copies, order_index, plate_index) "
            "VALUES (:p, :f, 1, 0, 0)"
        ),
        {"p": project.id, "f": file.id},
    )
    await db_session.commit()
    file_id, order_id = file.id, project.id

    await _run_upgrade(test_engine)
    db_session.expire_all()
    line = await _line_of(db_session, order_id)
    product_id = line.product_id
    # The conversion had nothing to read: a plate, and no parts on it.
    assert await _parts_of(db_session, product_id) == {}
    plates = (
        (await db_session.execute(select(ProductPlate).where(ProductPlate.product_id == product_id))).scalars().all()
    )
    assert [p.plate_index for p in plates] == [0]

    calls = await _seed_watching_for_orm(test_engine, monkeypatch)

    db_session.expire_all()
    row = (await db_session.execute(select(LibraryFile).where(LibraryFile.id == file_id))).scalar_one()
    assert sorted(row.file_metadata["printable_objects"].values()) == ["bracket.stl", "bracket.stl", "lid.stl"]
    parts = await _parts_of(db_session, product_id)
    assert {key: part.qty_per_unit for key, part in parts.items()} == {"bracket.stl": 2, "lid.stl": 1}
    assert all(part.auto for part in parts.values())
    assert calls == []

    # A ``DEBUG=true`` re-run must add nothing: the worklist query is the marker,
    # and the parts step only ever ADDS a key no part covers.
    stamp = row.updated_at
    await _run_seed(test_engine)
    db_session.expire_all()
    again = (await db_session.execute(select(LibraryFile).where(LibraryFile.id == file_id))).scalar_one()
    assert again.updated_at == stamp
    assert len(await _parts_of(db_session, product_id)) == 2


@pytest.mark.asyncio
async def test_an_operator_edited_part_survives_the_seed(db_session, test_engine, tmp_path):
    """The parts step only ADDS. A key already covered by a part's ``name_key``
    or by one of its aliases is left exactly as the operator left it — the seed
    is not a re-derivation of the product."""
    target = tmp_path / "kit.gcode.3mf"
    target.write_bytes(_sliced_3mf({1: ["bracket.stl", "bracket.stl", "lid.stl"]}))
    file = LibraryFile(
        filename="kit.gcode.3mf", file_path=str(target), file_size=1, file_type="gcode", file_metadata=None
    )
    product = Product(name="Bracket kit")
    db_session.add_all([file, product])
    await db_session.flush()
    db_session.add(ProductPlate(product_id=product.id, library_file_id=file.id, plate_index=0))
    db_session.add(
        ProductPart(
            product_id=product.id,
            kind="printed",
            name="Bracket",
            name_key="bracket",
            qty_per_unit=7,
            aliases=["bracket.stl"],
            auto=False,
        )
    )
    await db_session.execute(insert(product_files).values(product_id=product.id, library_file_id=file.id))
    await db_session.commit()
    product_id = product.id

    await _run_seed(test_engine)
    db_session.expire_all()

    parts = await _parts_of(db_session, product_id)
    assert set(parts) == {"bracket", "lid.stl"}
    assert parts["bracket"].qty_per_unit == 7 and parts["bracket"].auto is False
    assert parts["lid.stl"].qty_per_unit == 1 and parts["lid.stl"].auto is True


@pytest.mark.asyncio
async def test_the_seed_survives_a_library_file_whose_mount_is_down(db_session, test_engine, monkeypatch):
    """An unreachable path is skipped and retried at the next start — it must
    never take the upgrade down, and it must never be recorded as "no objects"."""
    from backend.app.services import library_objects_backfill as _backfill

    file = LibraryFile(
        filename="offline.gcode.3mf",
        file_path="//nas/share/offline.gcode.3mf",
        file_size=1,
        file_type="gcode",
        file_metadata={"print_time_seconds": 900},
    )
    db_session.add(file)
    await db_session.commit()
    file_id = file.id

    seen: dict = {}
    real = _backfill.backfill_library_objects

    async def capture(session_factory, **kwargs):
        seen["summary"] = await real(session_factory, **kwargs)
        return seen["summary"]

    monkeypatch.setattr(_backfill, "backfill_library_objects", capture)

    await _run_seed(test_engine)

    assert seen["summary"].skipped_unreachable == 1
    assert seen["summary"].filled == 0
    db_session.expire_all()
    row = (await db_session.execute(select(LibraryFile).where(LibraryFile.id == file_id))).scalar_one()
    assert row.file_metadata == {"print_time_seconds": 900}


_KNOWN_OBJECTS = {"101": "bracket.stl", "102": "bracket.stl", "103": "lid.stl"}


async def _legacy_order_for(db, tmp_path, label: str, *, metadata: dict | None, copies: int) -> int:
    """One legacy project + plan row against its own copy of the same 3MF."""
    target = tmp_path / f"{label}.gcode.3mf"
    target.write_bytes(_sliced_3mf({1: ["bracket.stl", "bracket.stl", "lid.stl"]}))
    file = LibraryFile(
        filename=f"{label}.gcode.3mf",
        file_path=str(target),
        file_size=1,
        file_type="gcode",
        file_metadata=metadata,
    )
    project = Project(name=f"{label} order", status="active")
    db.add_all([file, project])
    await db.flush()
    await db.execute(text("UPDATE projects SET is_template = 0 WHERE id = :id"), {"id": project.id})
    await db.execute(
        text("INSERT INTO library_file_projects (file_id, project_id) VALUES (:f, :p)"),
        {"f": file.id, "p": project.id},
    )
    await db.execute(
        text(
            "INSERT INTO project_print_plan_items (project_id, library_file_id, copies, order_index, plate_index) "
            "VALUES (:p, :f, :c, 0, 0)"
        ),
        {"p": project.id, "f": file.id, "c": copies},
    )
    return project.id


@pytest.mark.asyncio
async def test_the_seeded_parts_keep_the_legacy_plans_copies(db_session, test_engine, tmp_path):
    """⚠️ A converted part's ``qty_per_unit`` is Σ(copies × instances) — what the
    conversion writes for a derived part, and what rule B divides its targets by.

    ``copies`` is gone by the time ``seed()`` runs: ``_drop_legacy`` drops
    ``project_print_plan_items`` inside the same ``upgrade()`` transaction that
    read it. So the conversion hands the factor forward for every plate it could
    not measure, and the parts step applies it. Seeding the bare instance count
    instead would understate the per-unit need of exactly the population this
    backfill exists for, by the copies factor, silently.

    **The twin is the assertion**: the same plan against a file that HAD its
    metadata at conversion time must come out with the same parts.
    """
    async with test_engine.begin() as conn:
        for ddl in _LEGACY_DDL:
            await conn.execute(text(ddl))

    orders = {
        "empty": await _legacy_order_for(db_session, tmp_path, "empty", metadata=None, copies=2),
        "known": await _legacy_order_for(
            db_session,
            tmp_path,
            "known",
            metadata={
                "printable_objects": _KNOWN_OBJECTS,
                "plates": [{"index": 1, "printable_objects": _KNOWN_OBJECTS}],
            },
            copies=2,
        ),
    }
    await db_session.commit()

    await _run_upgrade(test_engine)
    await _run_seed(test_engine)
    db_session.expire_all()

    async def _qty(order_id: int) -> dict:
        line = await _line_of(db_session, order_id)
        return {key: part.qty_per_unit for key, part in (await _parts_of(db_session, line.product_id)).items()}

    known = await _qty(orders["known"])
    assert known == {"bracket.stl": 4, "lid.stl": 2}  # 2 copies × 2 and × 1
    assert await _qty(orders["empty"]) == known

    # The hand-over is spent and gone; a re-run finds no table and changes nothing.
    async with test_engine.begin() as conn:
        assert not await table_exists(conn, m158._PENDING_COPIES)
    await _run_seed(test_engine)
    db_session.expire_all()
    assert await _qty(orders["empty"]) == known


@pytest.mark.asyncio
async def test_a_seed_re_entered_after_the_backfill_still_gets_its_parts(db_session, test_engine, tmp_path):
    """⚠️ ``seed()`` can be re-entered with NOTHING left to fill, and must still
    seed the parts the conversion could not derive.

    ``library_objects_backfill._write_chunk`` COMMITS per chunk. So a ``seed()``
    killed after the chunks landed and before the parts step comes back to files
    that are already filled — an EMPTY ``filled_ids`` — and the runner re-enters
    from the top because no ``_migrations`` row was written. Gating the parts
    step on ``filled_ids`` alone therefore skipped it on exactly the run that had
    to do it, and then dropped the hand-over table at the bottom of the same
    function: the ``copies`` factor gone, the product part-less, forever.

    The widened gate is ``filled_ids ∪ the files named in _PENDING_COPIES``, and
    that second set is by construction "plates the conversion could not measure",
    so no curated product is walked (its twin test above still holds).

    The interruption is simulated the way it really happens: the backfill is run
    through the service FIRST, so the ``seed()`` below finds the metadata already
    there and reports nothing filled.
    """
    async with test_engine.begin() as conn:
        for ddl in _LEGACY_DDL:
            await conn.execute(text(ddl))
    order_id = await _legacy_order_for(db_session, tmp_path, "interrupted", metadata=None, copies=2)
    await db_session.commit()

    await _run_upgrade(test_engine)

    # The half that survived the interruption: the chunks committed.
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    first_pass = await backfill_library_objects(maker, sync_products=False)
    assert first_pass.filled == 1, "the fixture must give the re-entry something already filled"

    # ...and now the run that comes back to a worklist with nothing in it.
    async with test_engine.begin() as conn:
        assert await table_exists(conn, m158._PENDING_COPIES), "the hand-over must outlive the interruption"
    await _run_seed(test_engine)
    db_session.expire_all()

    line = await _line_of(db_session, order_id)
    parts = await _parts_of(db_session, line.product_id)
    # 2 copies × 2 brackets, 2 copies × 1 lid — the factor applied, not lost.
    assert {key: part.qty_per_unit for key, part in parts.items()} == {"bracket.stl": 4, "lid.stl": 2}


@pytest.mark.asyncio
async def test_the_parts_step_only_walks_the_files_this_run_filled(db_session, test_engine, tmp_path):
    """⚠️ A product whose file already knows its objects is not this run's
    business. Walking every product plate would put back an ``auto`` part the
    operator deleted — a migration quietly editing a composition somebody
    curated, on a product it never touched."""
    curated_file = LibraryFile(
        filename="curated.gcode.3mf",
        file_path=str(tmp_path / "curated.gcode.3mf"),
        file_size=1,
        file_type="gcode",
        file_metadata={"printable_objects": _KNOWN_OBJECTS},
    )
    (tmp_path / "curated.gcode.3mf").write_bytes(_sliced_3mf({1: ["bracket.stl"]}))
    empty_file = LibraryFile(
        filename="empty.gcode.3mf",
        file_path=str(tmp_path / "empty.gcode.3mf"),
        file_size=1,
        file_type="gcode",
        file_metadata=None,
    )
    (tmp_path / "empty.gcode.3mf").write_bytes(_sliced_3mf({1: ["knob.stl"]}))
    curated = Product(name="Curated")
    filled = Product(name="Filled")
    db_session.add_all([curated_file, empty_file, curated, filled])
    await db_session.flush()
    db_session.add(ProductPlate(product_id=curated.id, library_file_id=curated_file.id, plate_index=0))
    db_session.add(ProductPlate(product_id=filled.id, library_file_id=empty_file.id, plate_index=0))
    await db_session.commit()
    curated_id, filled_id = curated.id, filled.id

    await _run_seed(test_engine)
    db_session.expire_all()

    # The file this run filled got its parts; the curated product was not read.
    assert set(await _parts_of(db_session, filled_id)) == {"knob.stl"}
    assert await _parts_of(db_session, curated_id) == {}


@pytest.mark.asyncio
async def test_a_file_trashed_between_the_two_steps_seeds_no_parts(db_session, test_engine, tmp_path):
    """The fill and the parts step are separate transactions. A file the user
    trashes in between must plant nothing — its plates are not printable any
    more, the same rule ``recipes_for_products`` holds."""
    from datetime import datetime, timezone

    file = LibraryFile(
        filename="trashed.gcode.3mf",
        file_path=str(tmp_path / "trashed.gcode.3mf"),
        file_size=1,
        file_type="gcode",
        file_metadata={"printable_objects": _KNOWN_OBJECTS},
        deleted_at=datetime.now(timezone.utc),
    )
    product = Product(name="Trashed source")
    db_session.add_all([file, product])
    await db_session.flush()
    db_session.add(ProductPlate(product_id=product.id, library_file_id=file.id, plate_index=0))
    await db_session.commit()
    file_id, product_id = file.id, product.id

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        created = await m158._seed_printed_parts_from_plates(session, [file_id])
        await session.commit()

    assert created == 0
    db_session.expire_all()
    assert await _parts_of(db_session, product_id) == {}


@pytest.mark.asyncio
async def test_the_parts_step_works_after_the_hand_over_table_is_gone(db_session, test_engine, tmp_path):
    """⚠️ The hand-over table is dropped at the END of ``seed()``, and ``seed()``
    can be re-entered: a first pass whose mount was down fills nothing and drops
    it, a later pass fills the files and must still seed their parts. Missing
    means "no pending copies" — one copy each, what a file linked to a product
    directly has always meant — never an error."""
    (tmp_path / "late.gcode.3mf").write_bytes(_sliced_3mf({1: ["knob.stl", "knob.stl"]}))
    file = LibraryFile(
        filename="late.gcode.3mf",
        file_path=str(tmp_path / "late.gcode.3mf"),
        file_size=1,
        file_type="gcode",
        file_metadata=None,
    )
    product = Product(name="Late arrival")
    db_session.add_all([file, product])
    await db_session.flush()
    db_session.add(ProductPlate(product_id=product.id, library_file_id=file.id, plate_index=0))
    await db_session.commit()
    product_id = product.id

    async with test_engine.begin() as conn:
        assert not await table_exists(conn, m158._PENDING_COPIES)

    await _run_seed(test_engine)
    db_session.expire_all()

    parts = await _parts_of(db_session, product_id)
    assert {key: part.qty_per_unit for key, part in parts.items()} == {"knob.stl": 2}
