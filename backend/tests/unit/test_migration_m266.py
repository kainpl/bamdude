"""m266 adds the Spoolman bindings' ``registered`` stamp — nullable, no backfill, idempotent.

Rows from before it keep NULL: the first confirming reader stamps them (trust on first read,
spec spoolman-binding-identity D4).
"""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m266_spoolman_binding_registered as m266


@pytest.mark.asyncio
async def test_adds_the_stamp_to_both_tables_once_and_leaves_rows_unstamped(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/m266.db")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE spoolman_slot_assignments (id INTEGER PRIMARY KEY, spoolman_spool_id INTEGER)"
        )
        await conn.exec_driver_sql(
            "CREATE TABLE spoolman_k_profile (id INTEGER PRIMARY KEY, spoolman_spool_id INTEGER)"
        )
        await conn.exec_driver_sql("INSERT INTO spoolman_slot_assignments (id, spoolman_spool_id) VALUES (1, 12)")
        await conn.exec_driver_sql("INSERT INTO spoolman_k_profile (id, spoolman_spool_id) VALUES (1, 12)")
    async with engine.begin() as conn:
        await m266.upgrade(conn)
        await m266.upgrade(conn)  # add_column skips a column that exists — a re-run (DEBUG=true) must not fail
        stamps = [
            (await conn.exec_driver_sql(f"SELECT spool_registered FROM {table}")).scalar_one()
            for table in ("spoolman_slot_assignments", "spoolman_k_profile")
        ]
    await engine.dispose()
    assert stamps == [None, None]
    assert (m266.version, m266.name) == (266, "spoolman_binding_registered")
