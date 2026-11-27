"""m269: the Notify! ownership tables (upstream Bambuddy v1.3.0, #3265), on SQLite."""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m269_notify_ownership as migration

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def engine(tmp_path):
    """A database as an upgrade finds it: providers, no Notify! tables."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'm269.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE notification_providers (id INTEGER PRIMARY KEY, name VARCHAR)")
    yield engine
    await engine.dispose()


async def _columns(conn, table):
    return {row[1] for row in (await conn.exec_driver_sql(f"PRAGMA table_info({table})")).all()}


async def _unique_column_sets(conn, table):
    found = set()
    for index in (await conn.exec_driver_sql(f"PRAGMA index_list({table})")).all():
        if index[2]:
            info = (await conn.exec_driver_sql(f"PRAGMA index_info('{index[1]}')")).all()
            found.add(tuple(row[2] for row in info))
    return found


async def _provider_fk(conn, table):
    return [
        (row[2], row[3], row[4], row[6])
        for row in (await conn.exec_driver_sql(f"PRAGMA foreign_key_list({table})")).all()
    ]


async def test_upgrade_creates_both_tables_and_is_repeat_safe(engine):
    from backend.app.models.notification_live_activity import NotificationLiveActivity
    from backend.app.models.notification_lock_screen_widget import NotificationLockScreenWidget

    for _ in range(2):
        async with engine.begin() as conn:
            await migration.upgrade(conn)

    async with engine.connect() as conn:
        assert await _columns(conn, "notification_live_activities") == {
            column.name for column in NotificationLiveActivity.__table__.columns
        }
        assert await _columns(conn, "notification_lock_screen_widgets") == {
            column.name for column in NotificationLockScreenWidget.__table__.columns
        }
        assert ("provider_id", "printer_id", "print_key") in await _unique_column_sets(
            conn, "notification_live_activities"
        )
        assert ("provider_id", "printer_id") in await _unique_column_sets(conn, "notification_lock_screen_widgets")
        # The provider owns its rows; the printer deliberately has no FK (the row outlives it).
        for table in ("notification_live_activities", "notification_lock_screen_widgets"):
            assert await _provider_fk(conn, table) == [("notification_providers", "provider_id", "id", "CASCADE")]
