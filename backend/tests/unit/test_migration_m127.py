"""m127: thresholds, their state, and the two silence columns.

The state lives in the row on purpose. ``_ams_alarm_cooldown`` in ``main.py``
keeps the same kind of state in a process dictionary, and a restart forgets
that it has already rung.
"""

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

# Everything m127 touches or points at. create_all cannot resolve a foreign key
# to a table it cannot see, so the referenced ones are here too.
_PREREQUISITE_TABLES = (
    "printer_locations",
    "smart_sensors",
    "notification_providers",
)


# What m127 adds to tables that already existed. They are dropped again below,
# because building the prerequisites from TODAY's models would hand the
# migration a database that already has its work done — and then every
# assertion about that work passes without the migration doing anything.
_COLUMNS_M127_ADDS = {
    "smart_sensors": ("silent_since", "silence_notified_at"),
    "notification_providers": ("on_sensor_threshold", "on_sensor_silent"),
}


async def _prepared(path):
    """A database as it stood BEFORE m127: the tables it alters, without the
    columns it adds.

    m127 both creates a table and ALTERS two, and ``add_column`` issues a bare
    ``ALTER TABLE`` — on an empty database that fails with "no such table". So
    the prerequisites are created from the models and then walked back.
    """
    from backend.app.core.database import Base
    from backend.app.models import (  # noqa: F401
        notification,
        printer_location,
        smart_sensor,
    )

    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[Base.metadata.tables[t] for t in _PREREQUISITE_TABLES],
        )
        # SQLite has had DROP COLUMN since 3.35, which this project relies on
        # elsewhere (``helpers.drop_column``). Since m157 the model no longer
        # declares the provider event columns at all, so drop only what
        # create_all actually produced.
        for table, columns in _COLUMNS_M127_ADDS.items():
            existing = await conn.run_sync(lambda c, t=table: {x["name"] for x in inspect(c).get_columns(t)})
            for column in columns:
                if column in existing:
                    await conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
    return engine


@pytest.mark.asyncio
async def test_the_prepared_database_really_lacks_the_columns(tmp_path):
    """A guard on the guard. Every assertion below is about work the migration
    does; if the fixture handed it a database that already had these columns,
    they would all pass while it did nothing."""
    engine = await _prepared(tmp_path / "guard.db")
    async with engine.begin() as conn:
        for table, columns in _COLUMNS_M127_ADDS.items():
            found = await conn.run_sync(lambda c, t=table: {x["name"] for x in inspect(c).get_columns(t)})
            assert not (set(columns) & found), (table, found)
    await engine.dispose()


def test_the_migration_declares_its_version_and_name():
    from backend.app.migrations import m127_sensor_thresholds as m

    assert m.version == 127
    assert m.name == "sensor_thresholds"


@pytest.mark.asyncio
async def test_it_creates_the_table_and_alters_the_two(tmp_path):
    from backend.app.migrations import m127_sensor_thresholds as m

    engine = await _prepared(tmp_path / "a.db")
    async with engine.begin() as conn:
        await m.upgrade(conn)

        found = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='smart_sensor_thresholds'")
        )
        assert found.scalar_one_or_none() == "smart_sensor_thresholds"

        def columns(sync_conn, table):
            return {c["name"] for c in inspect(sync_conn).get_columns(table)}

        sensors = await conn.run_sync(lambda c: columns(c, "smart_sensors"))
        assert {"silent_since", "silence_notified_at"} <= sensors

        providers = await conn.run_sync(lambda c: columns(c, "notification_providers"))
        assert {"on_sensor_threshold", "on_sensor_silent"} <= providers
    await engine.dispose()


@pytest.mark.asyncio
async def test_upgrade_is_idempotent(tmp_path):
    """DEBUG=true re-runs the newest migration on every start."""
    from backend.app.migrations import m127_sensor_thresholds as m

    engine = await _prepared(tmp_path / "b.db")
    async with engine.begin() as conn:
        await m.upgrade(conn)
        await m.upgrade(conn)
    await engine.dispose()


@pytest.mark.asyncio
async def test_one_threshold_per_sensor_and_quantity(tmp_path):
    """Two limits on the same quantity would make "the" state ambiguous."""
    from backend.app.migrations import m127_sensor_thresholds as m

    engine = await _prepared(tmp_path / "c.db")
    async with engine.begin() as conn:
        await m.upgrade(conn)
        names = [n for (n,) in await conn.execute(text("SELECT name FROM sqlite_master WHERE type='index'"))]
    assert any("sensor_kind" in n for n in names)
    await engine.dispose()


@pytest.mark.asyncio
async def test_the_models_and_the_migration_agree_on_the_columns(tmp_path):
    """A table is created in two places — the model for fresh installs and the
    migration for existing ones. They drift in silence, and only one of them is
    exercised by everything else."""
    from backend.app.core.database import Base
    from backend.app.migrations import m127_sensor_thresholds as m
    from backend.app.models import (  # noqa: F401
        notification,
        printer_location,
        smart_sensor,
        smart_sensor_threshold,
    )

    # ``notification_providers`` left this comparison at m157: the model
    # stores subscriptions as JSON, and m127's two provider columns exist
    # only between m127 and m157 in the chain — pinned below instead.
    tables = ("smart_sensor_thresholds", "smart_sensors")

    def columns_of(sync_conn):
        inspector = inspect(sync_conn)
        return {t: {c["name"] for c in inspector.get_columns(t)} for t in tables}

    fresh = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
    async with fresh.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[Base.metadata.tables[t] for t in (*tables, "printer_locations")],
        )
        from_models = await conn.run_sync(columns_of)
    await fresh.dispose()

    upgraded = await _prepared(tmp_path / "upgraded.db")
    async with upgraded.begin() as conn:
        await m.upgrade(conn)
        from_migration = await conn.run_sync(columns_of)
    await upgraded.dispose()

    assert from_models == from_migration

    # The m157 squash must drop what m127 added, or a fresh replay leaves
    # orphan columns behind.
    from backend.app.migrations.m157_notifications_rework import _LEGACY_EVENT_COLUMNS

    assert set(_COLUMNS_M127_ADDS["notification_providers"]) <= set(_LEGACY_EVENT_COLUMNS)
