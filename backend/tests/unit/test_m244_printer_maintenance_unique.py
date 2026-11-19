"""m244 merges duplicate maintenance items and forbids new ones.

``printer_maintenance`` had no constraint on (printer, type) and its rows are
created on first read, so an installed database may hold several rows per pair.
The migration keeps ONE whole row per pair -- the one performed last, else the
lowest id -- with that row's own settings, moves the others' history onto it and
only then builds the unique index.
"""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m244_printer_maintenance_unique as m244

_ITEMS = """
    CREATE TABLE printer_maintenance (
        id INTEGER PRIMARY KEY,
        printer_id INTEGER NOT NULL,
        maintenance_type_id INTEGER NOT NULL,
        custom_interval_hours FLOAT,
        custom_interval_type VARCHAR(20),
        enabled BOOLEAN,
        last_performed_at DATETIME,
        last_performed_hours FLOAT
    )
"""
_HISTORY = """
    CREATE TABLE maintenance_history (
        id INTEGER PRIMARY KEY,
        printer_maintenance_id INTEGER NOT NULL REFERENCES printer_maintenance(id) ON DELETE CASCADE,
        notes TEXT
    )
"""
_INSERT = (
    "INSERT INTO printer_maintenance (id, printer_id, maintenance_type_id, custom_interval_hours, "
    "custom_interval_type, enabled, last_performed_at, last_performed_hours) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)


async def _engine(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm244.db'}")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_ITEMS)
        await conn.exec_driver_sql(_HISTORY)
    return engine


async def _items(conn):
    rows = await conn.exec_driver_sql(
        "SELECT id, printer_id, maintenance_type_id, custom_interval_hours, custom_interval_type, enabled, "
        "last_performed_at, last_performed_hours FROM printer_maintenance ORDER BY id"
    )
    return [tuple(row) for row in rows.fetchall()]


async def _history(conn):
    rows = await conn.exec_driver_sql("SELECT id, printer_maintenance_id FROM maintenance_history ORDER BY id")
    return [tuple(row) for row in rows.fetchall()]


@pytest.mark.asyncio
async def test_the_row_performed_last_stays_with_its_own_settings_and_everyones_history(tmp_path):
    engine = await _engine(tmp_path)
    async with engine.begin() as conn:
        # Pair (1, 7): the operator set an interval on row 1 and switched row 3 off; row 2 was performed last.
        await conn.exec_driver_sql(_INSERT, (1, 1, 7, 90.0, "days", 1, "2026-03-01 10:00:00.000000", 10.0))
        await conn.exec_driver_sql(_INSERT, (2, 1, 7, None, None, 1, "2026-05-01 10:00:00.000000", 55.5))
        await conn.exec_driver_sql(_INSERT, (3, 1, 7, 30.0, "hours", 0, None, 0.0))
        # Another printer's row of the same type, and another type of the same printer: not duplicates.
        await conn.exec_driver_sql(_INSERT, (4, 2, 7, 12.0, "hours", 0, None, 0.0))
        await conn.exec_driver_sql(_INSERT, (5, 1, 8, None, None, 1, "2026-01-01 00:00:00.000000", 1.0))
        for history_id, item_id in ((1, 1), (2, 2), (3, 3), (4, 1), (5, 4), (6, 5)):
            await conn.exec_driver_sql(
                "INSERT INTO maintenance_history (id, printer_maintenance_id) VALUES (?, ?)", (history_id, item_id)
            )

        await m244.upgrade(conn)

        assert await _items(conn) == [
            # Row 2 as it was: neither row 1's interval nor row 3's "off" is blended in.
            (2, 1, 7, None, None, 1, "2026-05-01 10:00:00.000000", 55.5),
            (4, 2, 7, 12.0, "hours", 0, None, 0.0),
            (5, 1, 8, None, None, 1, "2026-01-01 00:00:00.000000", 1.0),
        ]
        # Every history row of the pair now belongs to the kept row; the others are untouched.
        assert await _history(conn) == [(1, 2), (2, 2), (3, 2), (4, 2), (5, 4), (6, 5)]
    await engine.dispose()


@pytest.mark.asyncio
async def test_never_performed_duplicates_keep_the_lowest_id(tmp_path):
    engine = await _engine(tmp_path)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_INSERT, (10, 3, 2, 40.0, "hours", 0, None, 0.0))
        await conn.exec_driver_sql(_INSERT, (11, 3, 2, None, None, 1, None, 0.0))
        await conn.exec_driver_sql("INSERT INTO maintenance_history (id, printer_maintenance_id) VALUES (1, 11)")

        await m244.upgrade(conn)

        assert await _items(conn) == [(10, 3, 2, 40.0, "hours", 0, None, 0.0)]
        assert await _history(conn) == [(1, 10)]
    await engine.dispose()


@pytest.mark.asyncio
async def test_a_tie_on_the_last_performance_keeps_the_lowest_id(tmp_path):
    engine = await _engine(tmp_path)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_INSERT, (20, 4, 2, None, None, 1, "2026-05-01 10:00:00.000000", 5.0))
        await conn.exec_driver_sql(_INSERT, (21, 4, 2, 9.0, "days", 1, "2026-05-01 10:00:00.000000", 5.0))
        await conn.exec_driver_sql(_INSERT, (22, 4, 2, None, None, 1, "2026-04-30 23:59:59.999999", 4.0))

        await m244.upgrade(conn)

        assert [row[0] for row in await _items(conn)] == [20]
    await engine.dispose()


@pytest.mark.asyncio
async def test_the_index_refuses_a_second_row_of_a_pair_and_a_second_run_changes_nothing(tmp_path):
    engine = await _engine(tmp_path)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_INSERT, (1, 1, 7, None, None, 1, None, 0.0))
        await conn.exec_driver_sql(_INSERT, (2, 1, 7, None, None, 1, None, 0.0))

        await m244.upgrade(conn)

        indexes = {
            row[1]: bool(row[2])
            for row in (await conn.exec_driver_sql("PRAGMA index_list(printer_maintenance)")).fetchall()
        }
        assert indexes.get("uq_printer_maintenance_printer_type") is True
        columns = await conn.exec_driver_sql("PRAGMA index_info(uq_printer_maintenance_printer_type)")
        assert [row[2] for row in columns.fetchall()] == ["printer_id", "maintenance_type_id"]

        # idempotent (DEBUG=true re-runs the newest migration)
        await m244.upgrade(conn)
        assert [row[0] for row in await _items(conn)] == [1]

    async with engine.connect() as conn:
        with pytest.raises(IntegrityError):
            await conn.exec_driver_sql(_INSERT, (3, 1, 7, None, None, 1, None, 0.0))
        await conn.rollback()
        # Another printer or another type is still free.
        await conn.exec_driver_sql(_INSERT, (4, 2, 7, None, None, 1, None, 0.0))
        await conn.exec_driver_sql(_INSERT, (5, 1, 8, None, None, 1, None, 0.0))
        await conn.rollback()
    await engine.dispose()


@pytest.mark.asyncio
async def test_a_database_without_duplicates_only_gets_the_index(tmp_path):
    engine = await _engine(tmp_path)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_INSERT, (1, 1, 7, 5.0, "days", 0, None, 0.0))
        await conn.exec_driver_sql("INSERT INTO maintenance_history (id, printer_maintenance_id) VALUES (1, 1)")

        await m244.upgrade(conn)

        assert await _items(conn) == [(1, 1, 7, 5.0, "days", 0, None, 0.0)]
        assert await _history(conn) == [(1, 1)]
    await engine.dispose()


def test_the_model_declares_the_same_index():
    from backend.app.models.maintenance import PrinterMaintenance

    index = next(i for i in PrinterMaintenance.__table__.indexes if i.name == "uq_printer_maintenance_printer_type")
    assert index.unique is True
    assert [column.name for column in index.columns] == ["printer_id", "maintenance_type_id"]
