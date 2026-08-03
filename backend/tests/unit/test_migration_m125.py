"""Two history tables, and the reason their keys are wide.

Retention bounds how many rows a table holds. It does not bound the key
counter, which only ever grows — and on PostgreSQL the usual SERIAL is 32-bit.
At this write rate a large farm reaches that ceiling inside a decade.
"""

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine


def test_the_migration_declares_its_version_and_name():
    from backend.app.migrations import m125_measurement_history as m

    assert m.version == 125
    assert m.name == "measurement_history"


@pytest.mark.asyncio
async def test_both_tables_are_created(tmp_path):
    from backend.app.migrations import m125_measurement_history as m

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'a.db'}")
    async with engine.begin() as conn:
        await m.upgrade(conn)
        for table in ("smart_plug_power_history", "smart_sensor_history"):
            found = await conn.execute(text(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'"))
            assert found.scalar_one_or_none() == table
    await engine.dispose()


@pytest.mark.asyncio
async def test_upgrade_is_idempotent(tmp_path):
    """DEBUG=true re-runs the newest migration on every start."""
    from backend.app.migrations import m125_measurement_history as m

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'b.db'}")
    async with engine.begin() as conn:
        await m.upgrade(conn)
        await m.upgrade(conn)
    await engine.dispose()


@pytest.mark.asyncio
async def test_the_query_index_exists(tmp_path):
    """Every read is "this device, this window". Without the index that is a
    full scan of millions of rows, and it degrades as history accumulates —
    fast on the day it ships, slow a month later."""
    from backend.app.migrations import m125_measurement_history as m

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'c.db'}")
    async with engine.begin() as conn:
        await m.upgrade(conn)
        names = [n for (n,) in await conn.execute(text("SELECT name FROM sqlite_master WHERE type='index'"))]
    assert any("plug_power_history" in n for n in names)
    assert any("sensor_history" in n for n in names)
    await engine.dispose()


@pytest.mark.asyncio
async def test_the_models_and_the_migration_agree_on_the_columns(tmp_path):
    """A table is created in two places — the model for fresh installs and the
    migration for existing ones. They drift in silence, and only one of them is
    exercised by everything else."""
    from backend.app.core.database import Base
    from backend.app.migrations import m125_measurement_history as m

    # The referenced tables have to be on the metadata too, or create_all
    # cannot resolve the foreign keys — importing only the two new models
    # fails on a relationship it can see but not reach.
    from backend.app.models import (  # noqa: F401
        printer_location,
        smart_plug,
        smart_plug_power_history,
        smart_sensor,
        smart_sensor_history,
    )

    tables = ("smart_plug_power_history", "smart_sensor_history")

    def columns_of(sync_conn):
        inspector = inspect(sync_conn)
        return {t: {c["name"] for c in inspector.get_columns(t)} for t in tables}

    fresh = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
    async with fresh.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[Base.metadata.tables[t] for t in (*tables, "smart_plugs", "smart_sensors", "printer_locations")],
        )
        from_models = await conn.run_sync(columns_of)
    await fresh.dispose()

    upgraded = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'upgraded.db'}")
    async with upgraded.begin() as conn:
        await m.upgrade(conn)
        from_migration = await conn.run_sync(columns_of)
    await upgraded.dispose()

    assert from_models == from_migration


def test_the_keys_are_wide_enough_for_the_write_rate():
    """Pinned because it is invisible until it is not: a plain integer key on
    PostgreSQL stops the farm dead, years from now, with an insert that fails."""
    from sqlalchemy import BigInteger

    from backend.app.models.smart_plug_power_history import SmartPlugPowerHistory
    from backend.app.models.smart_sensor_history import SmartSensorHistory

    for model in (SmartPlugPowerHistory, SmartSensorHistory):
        assert isinstance(model.__table__.c.id.type, BigInteger), model.__name__
