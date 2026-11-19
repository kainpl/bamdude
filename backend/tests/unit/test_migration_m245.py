"""m245: the price history table and its one baseline row, on SQLite."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.migrations import m245_energy_price_history as migration

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def database(tmp_path):
    """A database as an upgrade finds it: a settings table, no price history."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'm245.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE settings (id INTEGER PRIMARY KEY, key VARCHAR UNIQUE, value TEXT)")
    yield engine, async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _migrate(engine, sessions):
    async with engine.begin() as conn:
        await migration.upgrade(conn)
    await migration.seed(sessions)


async def _history(sessions):
    async with sessions() as db:
        return (
            await db.execute(text("SELECT effective_at, price_per_kwh, source FROM energy_price_history ORDER BY id"))
        ).all()


async def _store(sessions, value):
    async with sessions() as db:
        await db.execute(text("INSERT INTO settings (key, value) VALUES ('energy_cost_per_kwh', :v)"), {"v": value})
        await db.commit()


async def test_the_baseline_carries_the_price_stored_at_upgrade_time_far_in_the_past(database):
    engine, sessions = database
    await _store(sessions, "4.32")

    await _migrate(engine, sessions)

    rows = await _history(sessions)
    assert len(rows) == 1
    effective_at, price, source = rows[0]
    assert (price, source) == (4.32, "baseline")
    assert str(effective_at).startswith("1970-01-01")


async def test_no_stored_price_means_the_settings_default(database):
    engine, sessions = database
    await _migrate(engine, sessions)
    assert [(price, source) for _at, price, source in await _history(sessions)] == [(0.15, "baseline")]


@pytest.mark.parametrize("stored", ["None", "abc", "nan", "-1"])
async def test_an_unreadable_stored_price_reads_as_the_default(database, stored):
    engine, sessions = database
    await _store(sessions, stored)
    await _migrate(engine, sessions)
    assert [(price, source) for _at, price, source in await _history(sessions)] == [(0.15, "baseline")]


async def test_a_second_run_changes_nothing(database):
    engine, sessions = database
    await _store(sessions, "0.2")
    await _migrate(engine, sessions)
    async with sessions() as db:
        await db.execute(text("UPDATE settings SET value = '0.9' WHERE key = 'energy_cost_per_kwh'"))
        await db.commit()

    await _migrate(engine, sessions)  # DEBUG re-runs the latest migration on every start

    assert [(price, source) for _at, price, source in await _history(sessions)] == [(0.2, "baseline")]


async def test_the_table_refuses_a_negative_price_and_an_unknown_source(database):
    engine, sessions = database
    await _migrate(engine, sessions)
    for values in ("('2026-01-01', -0.1, 'manual')", "('2026-01-01', 0.1, 'somebody')"):
        async with sessions() as db:
            with pytest.raises(Exception, match="CHECK|constraint"):
                await db.execute(
                    text(f"INSERT INTO energy_price_history (effective_at, price_per_kwh, source) VALUES {values}")
                )


async def test_the_model_and_the_migration_agree_on_the_columns(database):
    from backend.app.models.energy_price_history import EnergyPriceHistory

    engine, sessions = database
    await _migrate(engine, sessions)
    async with sessions() as db:
        columns = {row[1] for row in await db.execute(text("PRAGMA table_info(energy_price_history)"))}
    assert columns == {column.name for column in EnergyPriceHistory.__table__.columns}
