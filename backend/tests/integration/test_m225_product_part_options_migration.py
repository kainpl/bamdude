"""m225 — a part's option binding moves into ``product_part_options``; the old column goes.

Two shapes of ``product_parts`` reach m225 on SQLite, and both are built here: the
column m191 added to an older table (an inline REFERENCES, which SQLite's DROP COLUMN
takes once the column's index is gone), and the table ``create_all`` built on a fresh
install while the model still had the column (a table-level FOREIGN KEY, which DROP
COLUMN refuses outright).
"""

import pytest
import pytest_asyncio
from sqlalchemy import Boolean, Column, ForeignKey, Integer, MetaData, String, Table, UniqueConstraint, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m225_product_part_options as m225
from backend.app.migrations.helpers import column_exists
from backend.app.models.product import ProductPart  # noqa: F401 — registers product_parts for the FK below
from backend.app.models.product_variant import ProductPartOption

pytestmark = pytest.mark.asyncio

PRODUCT_ID = 1
OTHER_GROUP_ID = 10
GROUP_ID = 11  # the bound option's own group — not the product's first one
OTHER_OPTION_ID = 20
OPTION_ID = 21
PART_ID = 100
UNBOUND_ID = 101
DANGLING_ID = 102  # bound to an option that no longer exists
MISSING_OPTION_ID = 99
KEYS_BEFORE = [(1, ""), (2, "5d41402abc4b2a76")]
PARTS_COLUMNS = "id, product_id, kind, name, name_key, qty_per_unit, sort_order, ignored, image_source"


async def _parts_with_the_column_m191_added(conn):
    """m158's table, then m191's ``add_column`` and index — an install older than m191."""
    await conn.execute(
        text(
            """
            CREATE TABLE product_parts (
                id INTEGER PRIMARY KEY,
                product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                kind VARCHAR(16) NOT NULL DEFAULT 'printed',
                name VARCHAR(512) NOT NULL,
                name_key VARCHAR(512) NOT NULL,
                qty_per_unit INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 0,
                CONSTRAINT uq_product_parts_key UNIQUE (product_id, name_key)
            )
            """
        )
    )
    await conn.execute(text("CREATE INDEX ix_product_parts_product_id ON product_parts (product_id)"))
    await conn.execute(
        text(
            "ALTER TABLE product_parts ADD COLUMN variant_option_id INTEGER"
            " REFERENCES product_variant_options(id) ON DELETE SET NULL"
        )
    )
    await conn.execute(text("CREATE INDEX ix_product_parts_variant_option_id ON product_parts (variant_option_id)"))
    await conn.execute(text("ALTER TABLE product_parts ADD COLUMN ignored BOOLEAN NOT NULL DEFAULT 0"))
    await conn.execute(text("ALTER TABLE product_parts ADD COLUMN image_source VARCHAR(16) NOT NULL DEFAULT 'auto'"))


async def _parts_as_create_all_built_them(conn):
    """The table a fresh install got from the model between m191 and m225: SQLAlchemy writes
    every foreign key as a table constraint."""
    meta = MetaData()
    Table("products", meta, Column("id", Integer, primary_key=True))
    Table("product_variant_options", meta, Column("id", Integer, primary_key=True))
    parts = Table(
        "product_parts",
        meta,
        Column("id", Integer, primary_key=True),
        Column("product_id", Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True),
        Column("kind", String(16), nullable=False),
        Column("name", String(512), nullable=False),
        Column("name_key", String(512), nullable=False),
        Column("qty_per_unit", Integer, nullable=False, server_default="1"),
        Column("sort_order", Integer, nullable=False, server_default="0"),
        Column("ignored", Boolean, nullable=False, server_default="0"),
        Column(
            "variant_option_id",
            Integer,
            ForeignKey("product_variant_options.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        Column("image_source", String(16), nullable=False, server_default="auto"),
        UniqueConstraint("product_id", "name_key", name="uq_product_parts_key"),
    )
    await conn.run_sync(parts.create)
    ddl = (await conn.execute(text("SELECT sql FROM sqlite_master WHERE name = 'product_parts'"))).scalar()
    assert "FOREIGN KEY(variant_option_id)" in ddl  # the shape SQLite's DROP COLUMN refuses


@pytest_asyncio.fixture(
    params=[_parts_with_the_column_m191_added, _parts_as_create_all_built_them], ids=["m191", "create_all"]
)
async def legacy_db(request):
    """A database at m224 with one bound part, one unbound and one bound to a deleted option."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for ddl in (
            "CREATE TABLE products (id INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL)",
            "CREATE TABLE product_variant_groups (id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL"
            " REFERENCES products(id) ON DELETE CASCADE, name VARCHAR(128) NOT NULL)",
            "CREATE TABLE product_variant_options (id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL"
            " REFERENCES product_variant_groups(id) ON DELETE CASCADE, name VARCHAR(128) NOT NULL)",
            "CREATE TABLE project_lines (id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL,"
            " config_key VARCHAR(64) NOT NULL DEFAULT '')",
            "CREATE TABLE stock_items (id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL,"
            " config_key VARCHAR(64) NOT NULL DEFAULT '')",
        ):
            await conn.execute(text(ddl))
        await request.param(conn)
        # A table that points at product_parts: its reference must survive whatever m225 does to the table.
        await conn.execute(
            text(
                "CREATE TABLE product_part_stock_movements (id INTEGER PRIMARY KEY, product_part_id INTEGER NOT NULL"
                " REFERENCES product_parts(id) ON DELETE CASCADE, delta INTEGER NOT NULL)"
            )
        )
        await conn.execute(text("INSERT INTO products (id, name) VALUES (:p, 'Lamp')"), {"p": PRODUCT_ID})
        await conn.execute(
            text(
                "INSERT INTO product_variant_groups (id, product_id, name) VALUES (:a, :p, 'Tail'), (:b, :p, 'Shade')"
            ),
            {"a": OTHER_GROUP_ID, "b": GROUP_ID, "p": PRODUCT_ID},
        )
        await conn.execute(
            text(
                "INSERT INTO product_variant_options (id, group_id, name) VALUES (:a, :ga, 'Straight'), (:b, :gb, 'Cone')"
            ),
            {"a": OTHER_OPTION_ID, "ga": OTHER_GROUP_ID, "b": OPTION_ID, "gb": GROUP_ID},
        )
        await conn.execute(
            text(
                f"INSERT INTO product_parts ({PARTS_COLUMNS}, variant_option_id) VALUES"
                " (:bound, :p, 'printed', 'Cone', 'cone', 2, 0, 0, 'photo', :option),"
                " (:unbound, :p, 'printed', 'Base', 'base', 0, 1, 1, 'auto', NULL),"
                " (:dangling, :p, 'purchased', 'Bulb', 'purchased:bulb', 1, 2, 0, 'auto', :missing)"
            ),
            {
                "bound": PART_ID,
                "unbound": UNBOUND_ID,
                "dangling": DANGLING_ID,
                "p": PRODUCT_ID,
                "option": OPTION_ID,
                "missing": MISSING_OPTION_ID,
            },
        )
        await conn.execute(
            text("INSERT INTO product_part_stock_movements (id, product_part_id, delta) VALUES (1, :part, 3)"),
            {"part": PART_ID},
        )
        await conn.execute(
            text("INSERT INTO project_lines (id, product_id, config_key) VALUES (:a, :p, :ka), (:b, :p, :kb)"),
            {"a": KEYS_BEFORE[0][0], "ka": KEYS_BEFORE[0][1], "b": KEYS_BEFORE[1][0], "kb": KEYS_BEFORE[1][1], "p": 1},
        )
        await conn.execute(
            text("INSERT INTO stock_items (id, product_id, config_key) VALUES (5, :p, :k)"),
            {"p": PRODUCT_ID, "k": KEYS_BEFORE[1][1]},
        )
    try:
        yield engine
    finally:
        await engine.dispose()


async def _run(engine):
    async with engine.begin() as conn:
        await m225.upgrade(conn)


@pytest_asyncio.fixture
async def migrated_db(legacy_db):
    await _run(legacy_db)
    return legacy_db


async def fetch_all(engine, sql, **params):
    async with engine.connect() as conn:
        return [tuple(r) for r in (await conn.execute(text(sql), params)).all()]


async def _shape(engine, table):
    """A table's columns (cid left out — it moves when a column goes), foreign keys and named indexes."""
    columns = [(r[1], r[2], r[3], r[4], r[5]) for r in await fetch_all(engine, f"PRAGMA table_info({table})")]
    fks = {(r[2], r[3], r[4], r[6]) for r in await fetch_all(engine, f"PRAGMA foreign_key_list({table})")}
    indexes = {r[1] for r in await fetch_all(engine, f"PRAGMA index_list({table})")}
    return columns, fks, indexes


async def test_single_binding_becomes_one_row(migrated_db):
    rows = await fetch_all(migrated_db, "SELECT part_id, option_id, group_id FROM product_part_options")
    assert rows == [(PART_ID, OPTION_ID, GROUP_ID)]


async def test_unbound_part_gets_no_row(migrated_db):
    assert await fetch_all(migrated_db, "SELECT 1 FROM product_part_options WHERE part_id = :p", p=UNBOUND_ID) == []


async def test_a_binding_to_a_missing_option_moves_nothing(migrated_db):
    # No group to file it under; the service never lets a bound option be deleted, so none should exist.
    assert await fetch_all(migrated_db, "SELECT 1 FROM product_part_options WHERE part_id = :p", p=DANGLING_ID) == []


async def test_old_column_is_gone(migrated_db):
    async with migrated_db.connect() as conn:
        assert not await column_exists(conn, "product_parts", "variant_option_id")
    _, fks, indexes = await _shape(migrated_db, "product_parts")
    assert "ix_product_parts_variant_option_id" not in indexes
    assert all(fk[1] != "variant_option_id" for fk in fks)


async def test_saved_line_keys_do_not_move(migrated_db):
    assert await fetch_all(migrated_db, "SELECT id, config_key FROM project_lines ORDER BY id") == KEYS_BEFORE
    assert await fetch_all(migrated_db, "SELECT id, config_key FROM stock_items") == [(5, KEYS_BEFORE[1][1])]


async def test_the_rest_of_product_parts_survives(legacy_db):
    columns, fks, indexes = await _shape(legacy_db, "product_parts")
    rows = await fetch_all(legacy_db, f"SELECT {PARTS_COLUMNS} FROM product_parts ORDER BY id")
    await _run(legacy_db)

    after_columns, after_fks, after_indexes = await _shape(legacy_db, "product_parts")
    assert after_columns == [c for c in columns if c[0] != "variant_option_id"]
    assert after_fks == {fk for fk in fks if fk[1] != "variant_option_id"}
    assert ("products", "product_id", "id", "CASCADE") in after_fks
    assert after_indexes == indexes - {"ix_product_parts_variant_option_id"}
    assert "ix_product_parts_product_id" in after_indexes
    assert await fetch_all(legacy_db, f"SELECT {PARTS_COLUMNS} FROM product_parts ORDER BY id") == rows

    ddl = (await fetch_all(legacy_db, "SELECT sql FROM sqlite_master WHERE name = 'product_parts'"))[0][0]
    assert "uq_product_parts_key" in ddl
    async with legacy_db.begin() as conn:
        with pytest.raises(IntegrityError):
            await conn.execute(
                text(
                    "INSERT INTO product_parts (product_id, kind, name, name_key) VALUES (:p, 'printed', 'X', 'cone')"
                ),
                {"p": PRODUCT_ID},
            )

    # The table that points at product_parts still does, and keeps its row.
    movement_fks = (await _shape(legacy_db, "product_part_stock_movements"))[1]
    assert movement_fks == {("product_parts", "product_part_id", "id", "CASCADE")}
    assert await fetch_all(legacy_db, "SELECT product_part_id, delta FROM product_part_stock_movements") == [
        (PART_ID, 3)
    ]


async def test_the_table_matches_a_fresh_install(migrated_db):
    fresh = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with fresh.begin() as conn:
            await conn.run_sync(ProductPartOption.__table__.create)
        assert await _shape(migrated_db, "product_part_options") == await _shape(fresh, "product_part_options")
    finally:
        await fresh.dispose()
    columns, fks, indexes = await _shape(migrated_db, "product_part_options")
    assert [(c[0], c[2], c[4]) for c in columns] == [("part_id", 1, 1), ("option_id", 1, 2), ("group_id", 1, 0)]
    assert fks == {
        ("product_parts", "part_id", "id", "CASCADE"),
        ("product_variant_options", "option_id", "id", "CASCADE"),
        ("product_variant_groups", "group_id", "id", "CASCADE"),
    }
    assert "ix_product_part_options_group_id" in indexes


async def test_a_second_run_changes_nothing(migrated_db):
    before = await _shape(migrated_db, "product_parts")
    await _run(migrated_db)
    assert await fetch_all(migrated_db, "SELECT part_id, option_id, group_id FROM product_part_options") == [
        (PART_ID, OPTION_ID, GROUP_ID)
    ]
    assert await _shape(migrated_db, "product_parts") == before
