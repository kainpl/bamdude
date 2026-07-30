"""m119 gives the reorder alert its own memory, separate from the break one."""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m119_stock_reorder_notified

_CREATE = "CREATE TABLE filament_sku_settings (id INTEGER PRIMARY KEY, material VARCHAR(50))"


@pytest.mark.asyncio
async def test_m119_adds_the_column(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_CREATE)
        await m119_stock_reorder_notified.upgrade(conn)
        rows = await conn.exec_driver_sql("PRAGMA table_info(filament_sku_settings)")
        columns = {r[1] for r in rows.fetchall()}
    await engine.dispose()

    assert "stock_reorder_notified_at" in columns


@pytest.mark.asyncio
async def test_m119_is_idempotent(tmp_path):
    """Re-running must not raise — DEBUG=true re-runs the latest migration."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t2.db")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_CREATE)
        await m119_stock_reorder_notified.upgrade(conn)
        await m119_stock_reorder_notified.upgrade(conn)
    await engine.dispose()


@pytest.mark.asyncio
async def test_it_is_a_separate_column_from_the_break_stamp(tmp_path):
    """The two states are mutually exclusive; sharing one stamp would silence a
    SKU for a day exactly as it slid from "reorder now" into "will run out"."""
    from backend.app.migrations import m118_stock_break_notified

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t3.db")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_CREATE)
        await m118_stock_break_notified.upgrade(conn)
        await m119_stock_reorder_notified.upgrade(conn)
        rows = await conn.exec_driver_sql("PRAGMA table_info(filament_sku_settings)")
        columns = {r[1] for r in rows.fetchall()}
    await engine.dispose()

    assert {"stock_break_notified_at", "stock_reorder_notified_at"} <= columns


def test_m119_version_and_name():
    assert m119_stock_reorder_notified.version == 119
    assert m119_stock_reorder_notified.name == "stock_reorder_notified"


def test_model_declares_the_column_for_fresh_installs():
    from backend.app.models.filament_sku_settings import FilamentSkuSettings

    assert "stock_reorder_notified_at" in FilamentSkuSettings.__table__.columns
