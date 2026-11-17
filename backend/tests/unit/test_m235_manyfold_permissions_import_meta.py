"""m235: the import-origin table, and the Manyfold permissions for the groups that hold MakerWorld's."""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.migrations import m235_manyfold_permissions_import_meta as m235


@pytest.mark.asyncio
async def test_m235_creates_the_table_with_an_id_that_is_never_reused(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm235.db'}")
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE library_files (id INTEGER PRIMARY KEY, filename VARCHAR(255))")
        await m235.upgrade(conn)
        columns = {
            row[1]: (row[2], bool(row[3]))
            for row in (await conn.exec_driver_sql("PRAGMA table_info(library_file_import_meta)")).fetchall()
        }
        assert columns == {
            "id": ("INTEGER", False),
            "library_file_id": ("INTEGER", True),
            "source_type": ("VARCHAR(32)", True),
            "source_model_id": ("VARCHAR(128)", True),
            "source_file_id": ("VARCHAR(128)", False),
            "title": ("VARCHAR(500)", False),
            "creator": ("VARCHAR(255)", False),
            "license": ("VARCHAR(128)", False),
            "canonical_url": ("VARCHAR(1024)", False),
            "imported_at": ("DATETIME", True),
        }
        insert = (
            "INSERT INTO library_file_import_meta (library_file_id, source_type, source_model_id, imported_at) "
            "VALUES (1, 'manyfold', 'x7q3k2pdbm4z', '2026-01-01 00:00:00')"
        )
        await conn.exec_driver_sql(insert)
        with pytest.raises(IntegrityError):  # one record per library file
            await conn.exec_driver_sql(insert)
        await conn.exec_driver_sql("DELETE FROM library_file_import_meta")
        await conn.exec_driver_sql(insert)
        assert (await conn.exec_driver_sql("SELECT id FROM library_file_import_meta")).scalar() == 2
        await m235.upgrade(conn)  # idempotent
    await engine.dispose()


@pytest.mark.asyncio
async def test_postgresql_is_asked_for_types_it_has(monkeypatch):
    statements = []

    async def no_table(conn, name):
        return False

    class Conn:
        class dialect:
            name = "postgresql"

        async def exec_driver_sql(self, sql):
            statements.append(sql)

    monkeypatch.setattr(m235, "table_exists", no_table)
    await m235.upgrade(Conn())
    ddl = statements[0]
    assert "SERIAL PRIMARY KEY" in ddl and "TIMESTAMP NOT NULL" in ddl
    assert "AUTOINCREMENT" not in ddl and "DATETIME" not in ddl


def test_the_model_declares_the_same_columns():
    from backend.app.models.library_file_import_meta import LibraryFileImportMeta

    table = LibraryFileImportMeta.__table__
    assert [column.name for column in table.columns] == [
        "id",
        "library_file_id",
        "source_type",
        "source_model_id",
        "source_file_id",
        "title",
        "creator",
        "license",
        "canonical_url",
        "imported_at",
    ]
    assert table.dialect_options["sqlite"]["autoincrement"] is True


@pytest.mark.asyncio
async def test_the_seed_grants_manyfold_to_whoever_holds_makerworld_and_to_administrators(tmp_path):
    import json

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'seed.db'}")
    groups = {
        1: ("Administrators", 1, ["settings:update"]),
        2: ("Operators", 1, ["makerworld:view", "makerworld:import"]),
        3: ("Viewers", 1, ["makerworld:view"]),
        4: ("Night shift", 0, ["makerworld:import"]),
        5: ("Accounting", 0, ["orders:read"]),
        6: ("Already", 0, ["makerworld:view", "manyfold:view"]),
        7: ("Administrators", 0, []),  # a custom group that only shares the name
    }
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE groups (id INTEGER PRIMARY KEY, name VARCHAR, is_system BOOLEAN, permissions JSON)"
        )
        for group_id, (name, system, permissions) in groups.items():
            await conn.exec_driver_sql(
                "INSERT INTO groups (id, name, is_system, permissions) VALUES (?, ?, ?, ?)",
                (group_id, name, system, json.dumps(permissions)),
            )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await m235.seed(factory)
    await m235.seed(factory)  # twice: nothing is granted twice
    async with engine.connect() as conn:
        stored = {
            row[0]: json.loads(row[1])
            for row in (await conn.exec_driver_sql("SELECT id, permissions FROM groups")).fetchall()
        }
    await engine.dispose()
    assert stored[1] == ["settings:update", "manyfold:view", "manyfold:import"]
    assert stored[2] == ["makerworld:view", "makerworld:import", "manyfold:view", "manyfold:import"]
    assert stored[3] == ["makerworld:view", "manyfold:view"]
    assert stored[4] == ["makerworld:import", "manyfold:import"]
    assert stored[5] == ["orders:read"]
    assert stored[6] == ["makerworld:view", "manyfold:view"]
    assert stored[7] == []
