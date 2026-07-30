"""m117 gives the low-stock warning its memory."""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m117_low_stock_notified


@pytest.mark.asyncio
async def test_m117_adds_the_column(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    async with engine.begin() as conn:
        # Singular table name — the model is Spool, the table is `spool`.
        await conn.exec_driver_sql("CREATE TABLE spool (id INTEGER PRIMARY KEY, material VARCHAR(50))")
        await m117_low_stock_notified.upgrade(conn)
        rows = await conn.exec_driver_sql("PRAGMA table_info(spool)")
        columns = {r[1] for r in rows.fetchall()}
    await engine.dispose()

    assert "low_stock_notified" in columns


@pytest.mark.asyncio
async def test_m117_is_idempotent(tmp_path):
    """Re-running must not raise — DEBUG=true re-runs the latest migration."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t2.db")
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE spool (id INTEGER PRIMARY KEY, material VARCHAR(50))")
        await m117_low_stock_notified.upgrade(conn)
        await m117_low_stock_notified.upgrade(conn)
    await engine.dispose()


@pytest.mark.asyncio
async def test_existing_spools_start_unwarned(tmp_path):
    """So the first print after the upgrade catches up on the genuinely low
    ones — once each, then the flag holds."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t3.db")
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE spool (id INTEGER PRIMARY KEY, material VARCHAR(50))")
        await conn.exec_driver_sql("INSERT INTO spool (material) VALUES ('PLA')")
        await m117_low_stock_notified.upgrade(conn)
        row = (await conn.exec_driver_sql("SELECT low_stock_notified FROM spool")).fetchone()
    await engine.dispose()

    assert row[0] == 0


def test_m117_version_and_name():
    assert m117_low_stock_notified.version == 117
    assert m117_low_stock_notified.name == "low_stock_notified"


def test_model_declares_the_column_for_fresh_installs():
    from backend.app.models.spool import Spool

    assert "low_stock_notified" in Spool.__table__.columns
