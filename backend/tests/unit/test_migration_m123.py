"""m123 lands two tables and four permissions.

The permission half is the part that fails silently: our migrations are frozen
and Administrators are not self-healed at startup, so a permission that is not
seeded here is a permission nobody ever has.
"""

import pytest


def _our_tables():
    """Just this migration's two tables.

    Creating the whole metadata here would drag in every model another test
    happened to import and fail on their unresolved foreign keys — a failure
    about somebody else's schema, in a test about this one.
    """
    from backend.app.core.database import Base
    from backend.app.models import smart_sensor, zigbee_device  # noqa: F401

    return [Base.metadata.tables["zigbee_devices"], Base.metadata.tables["smart_sensors"]]


def test_the_migration_declares_its_version_and_name():
    from backend.app.migrations import m123_zigbee_device_settings as m

    assert m.version == 123
    assert m.name == "zigbee_device_settings"


def test_every_new_permission_is_seeded_to_administrators():
    """A drift guard for the O2 discipline, checked against the source of the
    seed rather than against a live DB."""
    from backend.app.migrations import m123_zigbee_device_settings as m

    assert set(m.NEW_PERMISSIONS) == {
        "smart_sensors:read",
        "smart_sensors:create",
        "smart_sensors:update",
        "smart_sensors:delete",
    }


def test_viewers_get_read_only():
    from backend.app.migrations import m123_zigbee_device_settings as m

    assert m.VIEWER_PERMISSIONS == ["smart_sensors:read"]


def test_the_seeded_names_are_the_ones_the_enum_actually_defines():
    """A typo in the seed string grants a permission no endpoint ever asks for,
    and the endpoint stays unreachable with nothing failing anywhere."""
    from backend.app.core.permissions import Permission
    from backend.app.migrations import m123_zigbee_device_settings as m

    defined = {p.value for p in Permission}
    assert set(m.NEW_PERMISSIONS) <= defined


@pytest.mark.asyncio
async def test_upgrade_is_idempotent(tmp_path):
    """DEBUG=true re-runs the latest migration on startup, so a second run must
    be a no-op rather than an error."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from backend.app.core.database import Base
    from backend.app.migrations import m123_zigbee_device_settings as m
    from backend.app.models import smart_sensor, zigbee_device  # noqa: F401

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_our_tables())
        await m.upgrade(conn)
        await m.upgrade(conn)
    await engine.dispose()


@pytest.mark.asyncio
async def test_upgrade_creates_the_tables_on_a_database_that_predates_them(tmp_path):
    """The path that matters: an existing install, where ``create_all`` has
    already run against the older models and will not add anything."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from backend.app.migrations import m123_zigbee_device_settings as m

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    async with engine.begin() as conn:
        await m.upgrade(conn)
        for table in ("zigbee_devices", "smart_sensors"):
            found = await conn.execute(text(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'"))
            assert found.scalar_one_or_none() == table
    await engine.dispose()


@pytest.mark.asyncio
async def test_the_models_and_the_migration_agree_on_the_columns(tmp_path):
    """Adding a table is TWO places — the model (fresh installs via create_all)
    and the migration (existing DBs). They drift silently: a fresh install and
    an upgraded one end up with different schemas, and only one of them is
    tested by everything else."""
    from sqlalchemy import inspect
    from sqlalchemy.ext.asyncio import create_async_engine

    from backend.app.core.database import Base
    from backend.app.migrations import m123_zigbee_device_settings as m

    # Importing the model modules is what puts them on Base.metadata — the app
    # does it lazily inside init_db() to avoid a models↔database import cycle,
    # so a fresh install gets these tables only because that block names them.
    from backend.app.models import smart_sensor, zigbee_device  # noqa: F401

    def columns_of(sync_conn):
        inspector = inspect(sync_conn)
        return {t: {c["name"] for c in inspector.get_columns(t)} for t in ("zigbee_devices", "smart_sensors")}

    fresh = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
    async with fresh.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_our_tables())
        from_models = await conn.run_sync(columns_of)
    await fresh.dispose()

    upgraded = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'upgraded.db'}")
    async with upgraded.begin() as conn:
        await m.upgrade(conn)
        from_migration = await conn.run_sync(columns_of)
    await upgraded.dispose()

    assert from_models == from_migration
