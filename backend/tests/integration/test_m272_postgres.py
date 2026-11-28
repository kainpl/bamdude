"""m272 on PostgreSQL: both CHECKs replaced under their own names; rows kept; the new slots admitted.

Destructive only to TEST_POSTGRES_URL (skipped without it).
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m272_spoolman_slot_rack_trays as m272
from backend.tests.integration.test_postgres_scenarios import (
    _async_url,
    _pg_url,
    _serialize_shared_postgres_target,  # noqa: F401
    _wipe,
)

pytestmark = pytest.mark.postgres


@pytest.fixture
async def engine():
    import asyncio

    url = _pg_url()
    await asyncio.to_thread(_wipe, url, all_objects=True)
    eng = create_async_engine(_async_url(url))
    yield eng
    await eng.dispose()


async def test_m272_on_postgres(engine, monkeypatch):
    from backend.app.migrations import helpers

    monkeypatch.setattr(m272, "is_postgres", lambda: True)
    monkeypatch.setattr(helpers, "is_postgres", lambda: True)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE printers (id SERIAL PRIMARY KEY)"))
        await conn.execute(text("INSERT INTO printers (id) VALUES (1)"))
        await conn.execute(
            text(
                "CREATE TABLE spoolman_slot_assignments ("
                "id SERIAL PRIMARY KEY, printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE, "
                "ams_id INTEGER NOT NULL, tray_id INTEGER NOT NULL, spoolman_spool_id INTEGER NOT NULL, "
                "spool_registered VARCHAR(40), assigned_at TIMESTAMP NOT NULL DEFAULT now(), "
                "CONSTRAINT uq_spoolman_slot_assignment UNIQUE (printer_id, ams_id, tray_id), "
                "CONSTRAINT ck_spoolman_slot_ams_id_range CHECK "
                "((ams_id >= 0 AND ams_id <= 7) OR (ams_id >= 128 AND ams_id <= 191) OR ams_id = 255), "
                "CONSTRAINT ck_spoolman_slot_tray_id_range CHECK (tray_id >= 0 AND tray_id <= 3))"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO spoolman_slot_assignments (printer_id, ams_id, tray_id, spoolman_spool_id) "
                "VALUES (1, 0, 3, 7), (1, 255, 1, 8)"
            )
        )
        await m272.upgrade(conn)
        names = (
            await conn.execute(
                text(
                    "SELECT conname FROM pg_constraint WHERE conrelid = 'spoolman_slot_assignments'::regclass "
                    "AND contype = 'c' ORDER BY conname"
                )
            )
        ).scalars()
        assert list(names) == ["ck_spoolman_slot_ams_id_range", "ck_spoolman_slot_tray_id_range"]
        await conn.execute(
            text(
                "INSERT INTO spoolman_slot_assignments (printer_id, ams_id, tray_id, spoolman_spool_id) "
                "VALUES (1, 32, 8, 9), (1, 33, 6, 10), (1, 255, 8, 11)"
            )
        )
        count = (await conn.execute(text("SELECT count(*) FROM spoolman_slot_assignments"))).scalar_one()
        assert count == 5
    async with engine.connect() as conn:
        with pytest.raises(IntegrityError):
            await conn.execute(
                text(
                    "INSERT INTO spoolman_slot_assignments (printer_id, ams_id, tray_id, spoolman_spool_id) "
                    "VALUES (1, 0, 4, 12)"
                )
            )
