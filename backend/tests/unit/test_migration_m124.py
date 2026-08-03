"""m124 turns three free-text columns into one entity.

The step that matters is the seed. An ``auto_queue_items.target_location`` may
name a place no printer has — which is exactly what a typo leaves behind — and
such an item routes NOWHERE today. Left NULL it would route ANYWHERE, silently,
on a live farm.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


def test_the_migration_declares_its_version_and_name():
    from backend.app.migrations import m124_printer_locations as m

    assert m.version == 124
    assert m.name == "printer_locations"


async def _legacy_db(path):
    """A database as it looked before this migration: three string columns."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE printers (id INTEGER PRIMARY KEY, name TEXT, location TEXT)"))
        await conn.execute(text("CREATE TABLE smart_sensors (id INTEGER PRIMARY KEY, name TEXT, location TEXT)"))
        await conn.execute(text("CREATE TABLE auto_queue_items (id INTEGER PRIMARY KEY, target_location TEXT)"))
    return engine


@pytest.mark.asyncio
async def test_every_distinct_value_from_all_three_tables_becomes_a_row(tmp_path):
    from backend.app.migrations import m124_printer_locations as m

    engine = await _legacy_db(tmp_path / "a.db")
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO printers (name, location) VALUES ('p1', 'Shop 1')"))
        await conn.execute(text("INSERT INTO smart_sensors (name, location) VALUES ('s1', 'Shop 2')"))
        await conn.execute(text("INSERT INTO auto_queue_items (target_location) VALUES ('Shop 3')"))
        await m.upgrade(conn)
        names = sorted(n for (n,) in await conn.execute(text("SELECT name FROM printer_locations")))

    assert names == ["Shop 1", "Shop 2", "Shop 3"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_differently_cased_names_collapse_to_one_place(tmp_path):
    from backend.app.migrations import m124_printer_locations as m

    engine = await _legacy_db(tmp_path / "b.db")
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO printers (name, location) VALUES ('p1', 'Цех 2')"))
        await conn.execute(text("INSERT INTO printers (name, location) VALUES ('p2', 'цех 2')"))
        await conn.execute(text("INSERT INTO printers (name, location) VALUES ('p3', ' Цех 2 ')"))
        await m.upgrade(conn)

        rows = (await conn.execute(text("SELECT COUNT(*) FROM printer_locations"))).scalar_one()
        ids = [i for (i,) in await conn.execute(text("SELECT DISTINCT location_id FROM printers"))]

    assert rows == 1, "three spellings, one place"
    assert len(ids) == 1, "and all three printers point at it"
    await engine.dispose()


@pytest.mark.asyncio
async def test_an_orphan_target_location_still_gets_a_row(tmp_path):
    """The dangerous one. This item matches no printer today and must go on
    matching no printer — NULL would mean "anywhere"."""
    from backend.app.migrations import m124_printer_locations as m

    engine = await _legacy_db(tmp_path / "c.db")
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO printers (name, location) VALUES ('p1', 'Shop 1')"))
        await conn.execute(text("INSERT INTO auto_queue_items (target_location) VALUES ('Shpo 1')"))
        await m.upgrade(conn)

        target = (await conn.execute(text("SELECT target_location_id FROM auto_queue_items"))).scalar_one()
        matching = (
            await conn.execute(text("SELECT COUNT(*) FROM printers WHERE location_id = :t"), {"t": target})
        ).scalar_one()

    assert target is not None, "an orphan must not become 'anywhere'"
    assert matching == 0, "and it must still match no printer"
    await engine.dispose()


@pytest.mark.asyncio
async def test_a_queue_item_aimed_at_a_real_place_keeps_matching_it(tmp_path):
    """The mirror of the test above: a target that DID match must go on
    matching, or the migration would unroute working setups instead."""
    from backend.app.migrations import m124_printer_locations as m

    engine = await _legacy_db(tmp_path / "c2.db")
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO printers (name, location) VALUES ('p1', 'Shop 1')"))
        await conn.execute(text("INSERT INTO auto_queue_items (target_location) VALUES ('Shop 1')"))
        await m.upgrade(conn)

        target = (await conn.execute(text("SELECT target_location_id FROM auto_queue_items"))).scalar_one()
        matching = (
            await conn.execute(text("SELECT COUNT(*) FROM printers WHERE location_id = :t"), {"t": target})
        ).scalar_one()

    assert matching == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_empty_and_null_locations_become_null_not_a_place(tmp_path):
    from backend.app.migrations import m124_printer_locations as m

    engine = await _legacy_db(tmp_path / "d.db")
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO printers (name, location) VALUES ('p1', '')"))
        await conn.execute(text("INSERT INTO printers (name, location) VALUES ('p2', '   ')"))
        await conn.execute(text("INSERT INTO printers (name, location) VALUES ('p3', NULL)"))
        await m.upgrade(conn)

        assert (await conn.execute(text("SELECT COUNT(*) FROM printer_locations"))).scalar_one() == 0
        assert (
            await conn.execute(text("SELECT COUNT(*) FROM printers WHERE location_id IS NOT NULL"))
        ).scalar_one() == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_the_string_columns_are_gone(tmp_path):
    from backend.app.migrations import m124_printer_locations as m
    from backend.app.migrations.helpers import get_table_columns

    engine = await _legacy_db(tmp_path / "e.db")
    async with engine.begin() as conn:
        await m.upgrade(conn)
        assert "location" not in await get_table_columns(conn, "printers")
        assert "location" not in await get_table_columns(conn, "smart_sensors")
        assert "target_location" not in await get_table_columns(conn, "auto_queue_items")
    await engine.dispose()


@pytest.mark.asyncio
async def test_upgrade_is_idempotent(tmp_path):
    """DEBUG=true re-runs the newest migration on every start."""
    from backend.app.migrations import m124_printer_locations as m

    engine = await _legacy_db(tmp_path / "f.db")
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO printers (name, location) VALUES ('p1', 'Shop 1')"))
        await m.upgrade(conn)
        await m.upgrade(conn)
        assert (await conn.execute(text("SELECT COUNT(*) FROM printer_locations"))).scalar_one() == 1
        assert (await conn.execute(text("SELECT location_id FROM printers"))).scalar_one() is not None
    await engine.dispose()


@pytest.mark.asyncio
async def test_the_model_and_the_migration_chain_agree_on_the_columns(tmp_path):
    """A table is created in two places — the model for fresh installs and the
    migrations for existing ones. They drift in silence, and only one of them is
    exercised by everything else.

    The CHAIN, not one migration: an existing database gets every migration in
    order, so comparing the model against m124 alone starts failing the moment a
    later one touches this table — as m126 did by adding ``parent_id``. A
    migration that touches ``printer_locations`` belongs in this list.
    """
    from sqlalchemy import inspect

    from backend.app.core.database import Base
    from backend.app.migrations import m124_printer_locations as m, m126_location_hierarchy as m126
    from backend.app.models import printer_location  # noqa: F401 — puts it on the metadata

    def columns_of(sync_conn):
        return {c["name"] for c in inspect(sync_conn).get_columns("printer_locations")}

    fresh = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
    async with fresh.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[Base.metadata.tables["printer_locations"]])
        from_model = await conn.run_sync(columns_of)
    await fresh.dispose()

    upgraded = await _legacy_db(tmp_path / "upgraded.db")
    async with upgraded.begin() as conn:
        await m.upgrade(conn)
        await m126.upgrade(conn)
        from_migration = await conn.run_sync(columns_of)
    await upgraded.dispose()

    assert from_model == from_migration
