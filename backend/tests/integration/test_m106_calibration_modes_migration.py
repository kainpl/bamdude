"""m106 — print_queue tri-state calibration-mode columns.

Covers the migration itself (adds nullable, no-default columns; idempotent),
the init_db boot self-check that guards against the ORM↔DB drift that broke
production once, and an ORM round-trip proving ``select(PrintQueueItem)`` works
with the new columns (NULL = derive-from-bool; 'auto' persists).
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.database import _verify_calibration_mode_columns
from backend.app.migrations import m106_print_queue_calibration_modes
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue

MODE_COLS = ("bed_levelling_mode", "flow_cali_mode", "nozzle_offset_cali_mode")


@pytest_asyncio.fixture
async def engine_bare_queue():
    """print_queue with the legacy bool columns but NOT the mode columns —
    the pre-m106 schema, so the migration and self-check have something to act on."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE print_queue ("
                "id INTEGER PRIMARY KEY, "
                "bed_levelling BOOLEAN DEFAULT 1, "
                "flow_cali BOOLEAN DEFAULT 1, "
                "nozzle_offset_cali BOOLEAN DEFAULT 1)"
            )
        )
    try:
        yield engine
    finally:
        await engine.dispose()


async def _cols(conn) -> set[str]:
    r = await conn.execute(text("PRAGMA table_info(print_queue)"))
    return {row[1] for row in r.fetchall()}


@pytest.mark.asyncio
async def test_m106_adds_nullable_no_default_mode_columns(engine_bare_queue):
    async with engine_bare_queue.begin() as conn:
        await m106_print_queue_calibration_modes.upgrade(conn)
        cols = await _cols(conn)
        assert all(c in cols for c in MODE_COLS)
        # Nullable + no default: a row inserted without them must succeed and
        # leave them NULL — the derive-from-bool sentinel every existing row
        # relies on (no backfill).
        await conn.execute(text("INSERT INTO print_queue (id, bed_levelling) VALUES (1, 0)"))
        r = await conn.execute(
            text("SELECT bed_levelling_mode, flow_cali_mode, nozzle_offset_cali_mode FROM print_queue WHERE id=1")
        )
        assert r.fetchone() == (None, None, None)


@pytest.mark.asyncio
async def test_m106_is_idempotent(engine_bare_queue):
    async with engine_bare_queue.begin() as conn:
        await m106_print_queue_calibration_modes.upgrade(conn)
        await m106_print_queue_calibration_modes.upgrade(conn)  # must not raise duplicate-column
        cols = await _cols(conn)
        assert all(c in cols for c in MODE_COLS)


@pytest.mark.asyncio
async def test_boot_self_check_raises_when_columns_missing(engine_bare_queue):
    # Pre-m106 schema (drifted) — the guard must refuse to serve.
    with pytest.raises(RuntimeError, match="calibration-mode column"):
        await _verify_calibration_mode_columns(engine_bare_queue)


@pytest.mark.asyncio
async def test_boot_self_check_passes_after_migration(engine_bare_queue):
    async with engine_bare_queue.begin() as conn:
        await m106_print_queue_calibration_modes.upgrade(conn)
    await _verify_calibration_mode_columns(engine_bare_queue)  # no raise once columns exist


@pytest.mark.asyncio
async def test_print_queue_item_mode_columns_round_trip(db_session):
    """ORM path: create items with mode NULL and mode='auto', select them back.

    This is the exact ``select(PrintQueueItem)`` that cascaded into a mid-print
    outage when the columns were mapped but absent from the DB — here they exist
    (create_all), so it must round-trip cleanly."""
    queue = PrinterQueue(printer_id=1, status="idle")
    db_session.add(queue)
    await db_session.flush()

    legacy = PrintQueueItem(queue_id=queue.id, status="pending")  # modes unset → NULL
    auto = PrintQueueItem(queue_id=queue.id, status="pending", bed_levelling_mode="auto")
    db_session.add_all([legacy, auto])
    await db_session.flush()

    rows = (await db_session.execute(select(PrintQueueItem).order_by(PrintQueueItem.id))).scalars().all()
    assert rows[0].bed_levelling_mode is None
    assert rows[0].flow_cali_mode is None
    assert rows[0].nozzle_offset_cali_mode is None
    assert rows[1].bed_levelling_mode == "auto"
