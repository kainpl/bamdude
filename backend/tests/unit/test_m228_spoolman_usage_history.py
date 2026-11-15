"""m228 creates ``spoolman_usage_history`` with the columns the model declares."""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m228_spoolman_usage_history as m228
from backend.app.models.spoolman_usage_history import SpoolmanUsageHistory


@pytest.mark.asyncio
async def test_m228_creates_the_table_the_model_describes(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm228.db'}")
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE printers (id INTEGER PRIMARY KEY)")
        await conn.exec_driver_sql("CREATE TABLE print_archives (id INTEGER PRIMARY KEY)")

        await m228.upgrade(conn)

        info = (await conn.exec_driver_sql("PRAGMA table_info(spoolman_usage_history)")).fetchall()
        migrated = {row[1]: (row[2].split("(")[0], bool(row[3])) for row in info}
        declared = {
            column.name: (str(column.type).split("(")[0], not column.nullable and not column.primary_key)
            for column in SpoolmanUsageHistory.__table__.columns
        }
        # Same names, same types; NOT NULL where the model has no default to lean on.
        assert set(migrated) == set(declared)
        for name, (type_name, _required) in declared.items():
            assert migrated[name][0] == type_name, name
        assert migrated["created_at"] == ("DATETIME", True)
        assert migrated["source_token"][1] and migrated["spoolman_spool_id"][1]
        assert not migrated["spool_registered"][1] and not migrated["archive_id"][1]

        indexes = {row[1] for row in (await conn.exec_driver_sql("PRAGMA index_list(spoolman_usage_history)"))}
        assert indexes >= {"ix_spoolman_usage_history_source_created", "ix_spoolman_usage_history_spool"}
        assert indexes >= {index.name for index in SpoolmanUsageHistory.__table__.indexes}

        # idempotent (DEBUG=true re-runs the newest migration)
        await m228.upgrade(conn)
    await engine.dispose()


@pytest.mark.asyncio
async def test_postgresql_is_asked_for_types_it_has():
    """PostgreSQL has neither DATETIME nor a bare INTEGER PRIMARY KEY that counts by itself."""
    from types import SimpleNamespace

    sent: list[str] = []

    class _Conn:
        dialect = SimpleNamespace(name="postgresql")

        async def exec_driver_sql(self, sql, *_args):
            sent.append(sql)
            return SimpleNamespace(first=lambda: None, scalar=lambda: None, fetchall=lambda: [])

        async def execute(self, *_args, **_kwargs):
            return SimpleNamespace(first=lambda: None, scalar=lambda: None, fetchall=lambda: [])

        async def run_sync(self, _fn):
            return False

    await m228.upgrade(_Conn())
    create = next(sql for sql in sent if "CREATE TABLE" in sql)
    assert "SERIAL PRIMARY KEY" in create and "created_at TIMESTAMP NOT NULL" in create
    assert "DATETIME" not in create
