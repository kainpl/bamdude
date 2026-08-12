"""m120 empties the queue tables and puts every printer queue back to idle.

The reset half is the one worth pinning. Emptying ``print_queue`` while leaving
``printer_queues.status='printing'`` would trade a cosmetic defect for a farm
that never takes work again: that column is the dispatch claim the scheduler
reads into ``busy_printers``, so a queue still claiming a printer with no items
to run is a printer parked forever.
"""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m120_clear_queues


async def _db(tmp_path, name: str):
    """A miniature of the four tables m120 touches."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/{name}")
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE print_queue (id INTEGER PRIMARY KEY, queue_id INTEGER, status TEXT)")
        await conn.exec_driver_sql("CREATE TABLE auto_queue_items (id INTEGER PRIMARY KEY, status TEXT)")
        await conn.exec_driver_sql(
            "CREATE TABLE printer_queues (id INTEGER PRIMARY KEY, status TEXT, current_item_id INTEGER, "
            "pending_count INTEGER, skipped_count INTEGER, is_paused BOOLEAN, auto_distribute_eligible BOOLEAN)"
        )
        await conn.exec_driver_sql(
            "CREATE TABLE printers (id INTEGER PRIMARY KEY, name TEXT, mqtt_connection_timeout INTEGER)"
        )
    return engine


@pytest.mark.asyncio
async def test_both_queue_tables_end_up_empty(tmp_path):
    engine = await _db(tmp_path, "a.db")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "INSERT INTO print_queue (id, queue_id, status) VALUES (1, 1, 'printing'), (2, 1, 'printing'), "
            "(3, 2, 'pending')"
        )
        await conn.exec_driver_sql("INSERT INTO auto_queue_items (id, status) VALUES (1, 'pending'), (2, 'routed')")
        await conn.exec_driver_sql(
            "INSERT INTO printer_queues (id, status, current_item_id, pending_count, skipped_count, is_paused, "
            "auto_distribute_eligible) VALUES (1, 'printing', 1, 2, 1, 0, 1)"
        )
        await m120_clear_queues.upgrade(conn)

        assert (await conn.exec_driver_sql("SELECT COUNT(*) FROM print_queue")).scalar() == 0
        assert (await conn.exec_driver_sql("SELECT COUNT(*) FROM auto_queue_items")).scalar() == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_a_queue_left_claiming_a_printer_is_released(tmp_path):
    """The load-bearing half: status is what the scheduler treats as busy."""
    engine = await _db(tmp_path, "b.db")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "INSERT INTO printer_queues (id, status, current_item_id, pending_count, skipped_count, is_paused, "
            "auto_distribute_eligible) VALUES (1, 'printing', 77, 5, 3, 0, 1)"
        )
        await m120_clear_queues.upgrade(conn)

        row = (
            await conn.exec_driver_sql(
                "SELECT status, current_item_id, pending_count, skipped_count FROM printer_queues WHERE id = 1"
            )
        ).fetchone()

    assert row == ("idle", None, 0, 0)
    await engine.dispose()


@pytest.mark.asyncio
async def test_operator_configuration_survives(tmp_path):
    """``is_paused`` and ``auto_distribute_eligible`` are choices someone made,
    not run state. Resetting them would silently put a printer the operator had
    set aside back into rotation."""
    engine = await _db(tmp_path, "c.db")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "INSERT INTO printer_queues (id, status, current_item_id, pending_count, skipped_count, is_paused, "
            "auto_distribute_eligible) VALUES (1, 'printing', 9, 1, 1, 1, 0)"
        )
        await m120_clear_queues.upgrade(conn)

        row = (
            await conn.exec_driver_sql("SELECT is_paused, auto_distribute_eligible FROM printer_queues WHERE id = 1")
        ).fetchone()

    assert row == (1, 0), "a paused, non-distributable queue must stay paused and non-distributable"
    await engine.dispose()


@pytest.mark.asyncio
async def test_mqtt_connection_recycling_is_disabled_on_every_printer(tmp_path):
    """Every printer, not only the ones that showed the symptom.

    ``ensure_fresh_connection`` discards a printer's MQTT client once the link
    is older than this value, and the recycling is unconditional — a farm that
    happened not to lose a swap macro to it this week is not any safer from it.
    Zero is the disabled sentinel the manager checks (``timeout <= 0`` returns
    early without touching the client).
    """
    engine = await _db(tmp_path, "e.db")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "INSERT INTO printers (id, name, mqtt_connection_timeout) VALUES (1, 'X1C', 900), (2, 'P1S', 300), "
            "(3, 'A1', 0)"
        )
        await m120_clear_queues.upgrade(conn)

        rows = (await conn.exec_driver_sql("SELECT mqtt_connection_timeout FROM printers ORDER BY id")).fetchall()

    assert [row[0] for row in rows] == [0, 0, 0]
    await engine.dispose()


@pytest.mark.asyncio
async def test_it_is_idempotent(tmp_path):
    """DEBUG=true re-runs the latest migration on every start, so this one has to
    survive being applied to a database it has already emptied."""
    engine = await _db(tmp_path, "d.db")
    async with engine.begin() as conn:
        await conn.exec_driver_sql("INSERT INTO print_queue (id, queue_id, status) VALUES (1, 1, 'printing')")
        await conn.exec_driver_sql(
            "INSERT INTO printer_queues (id, status, current_item_id, pending_count, skipped_count, is_paused, "
            "auto_distribute_eligible) VALUES (1, 'printing', 1, 1, 0, 0, 1)"
        )
        await m120_clear_queues.upgrade(conn)
        await m120_clear_queues.upgrade(conn)  # must not raise

        assert (await conn.exec_driver_sql("SELECT COUNT(*) FROM print_queue")).scalar() == 0
    await engine.dispose()


def test_version_and_name():
    assert m120_clear_queues.version == 120
    assert m120_clear_queues.name == "clear_queues"
