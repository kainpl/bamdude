"""m118 gives the stock-break alert its memory."""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m118_stock_break_notified

_CREATE = "CREATE TABLE filament_sku_settings (id INTEGER PRIMARY KEY, material VARCHAR(50))"


@pytest.mark.asyncio
async def test_m118_adds_the_column(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_CREATE)
        await m118_stock_break_notified.upgrade(conn)
        rows = await conn.exec_driver_sql("PRAGMA table_info(filament_sku_settings)")
        columns = {r[1] for r in rows.fetchall()}
    await engine.dispose()

    assert "stock_break_notified_at" in columns


@pytest.mark.asyncio
async def test_m118_is_idempotent(tmp_path):
    """Re-running must not raise — DEBUG=true re-runs the latest migration."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t2.db")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_CREATE)
        await m118_stock_break_notified.upgrade(conn)
        await m118_stock_break_notified.upgrade(conn)
    await engine.dispose()


@pytest.mark.asyncio
async def test_existing_rows_start_unannounced(tmp_path):
    """NULL, not "now": on the first pass after upgrade a SKU genuinely in
    break should say so once rather than wait out a repeat window it never had."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t3.db")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_CREATE)
        await conn.exec_driver_sql("INSERT INTO filament_sku_settings (material) VALUES ('PLA')")
        await m118_stock_break_notified.upgrade(conn)
        row = (await conn.exec_driver_sql("SELECT stock_break_notified_at FROM filament_sku_settings")).fetchone()
    await engine.dispose()

    assert row[0] is None


def test_m118_version_and_name():
    assert m118_stock_break_notified.version == 118
    assert m118_stock_break_notified.name == "stock_break_notified"


def test_model_declares_the_column_for_fresh_installs():
    from backend.app.models.filament_sku_settings import FilamentSkuSettings

    assert "stock_break_notified_at" in FilamentSkuSettings.__table__.columns
