"""m275 on PostgreSQL: H3C / P3C rows saved before stage A cleared or sent to review, nothing else touched.

The tables are the minimum the seed reads (``printers``, ``print_queue``,
``queue_sources``, ``print_archives``, ``library_files``), with PostgreSQL's
own types — a real ``BOOLEAN`` the SQLite arm cannot exercise.
Destructive only to TEST_POSTGRES_URL (skipped without it).
"""

import json

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.migrations import m275_rack_queue_rows_review as m275
from backend.tests.integration.test_postgres_scenarios import (
    _async_url,
    _pg_url,
    _serialize_shared_postgres_target,  # noqa: F401
    _wipe,
)

pytestmark = pytest.mark.postgres

AUTO = json.dumps({"version": 2, "mode": "auto", "feed_policy": "auto"})


@pytest.fixture
async def engine():
    import asyncio

    url = _pg_url()
    await asyncio.to_thread(_wipe, url, all_objects=True)
    eng = create_async_engine(_async_url(url))
    yield eng
    await eng.dispose()


async def test_m275_on_postgres(engine):
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE printers (id SERIAL PRIMARY KEY, model VARCHAR(50))"))
        await conn.execute(text("CREATE TABLE queue_sources (id SERIAL PRIMARY KEY, format VARCHAR(10))"))
        await conn.execute(text("CREATE TABLE print_archives (id SERIAL PRIMARY KEY, file_path VARCHAR(500))"))
        await conn.execute(text("CREATE TABLE library_files (id SERIAL PRIMARY KEY, file_path VARCHAR(500))"))
        await conn.execute(
            text(
                "CREATE TABLE print_queue (id SERIAL PRIMARY KEY, queue_id INTEGER NOT NULL, status VARCHAR(20), "
                "ams_mapping TEXT, filament_routing TEXT, is_calibration BOOLEAN NOT NULL DEFAULT false, "
                "source_auto_item_id INTEGER, queue_source_id INTEGER, archive_id INTEGER, library_file_id INTEGER)"
            )
        )
        await conn.execute(text("INSERT INTO printers (id, model) VALUES (1, 'Bambu Lab H3C'), (2, 'X1C')"))
        await conn.execute(text("INSERT INTO queue_sources (id, format) VALUES (1, 'gcode')"))
        await conn.execute(
            text(
                "INSERT INTO print_queue (id, queue_id, status, ams_mapping, filament_routing, is_calibration, "
                "queue_source_id) VALUES "
                "(1, 1, 'pending', '[131]', :auto, false, NULL), "  # automatic: plan cleared
                "(2, 1, 'pending', '[131]', :auto, false, 1), "  # raw G-code: review
                "(3, 1, 'pending', '[131]', NULL, true, NULL), "  # calibration, no intent: review
                "(4, 1, 'completed', '[131]', :auto, false, NULL), "  # history: untouched
                "(5, 2, 'pending', '[131]', :auto, false, NULL)"  # an X1C: untouched
            ),
            {"auto": AUTO},
        )
    await m275.seed(async_sessionmaker(engine, expire_on_commit=False))
    await m275.seed(async_sessionmaker(engine, expire_on_commit=False))  # a rerun changes nothing
    async with engine.connect() as conn:
        rows = {
            r.id: (r.ams_mapping, r.filament_routing)
            for r in await conn.execute(text("SELECT id, ams_mapping, filament_routing FROM print_queue"))
        }
    assert rows[1] == (None, AUTO)
    assert rows[2][0] == "[131]" and json.loads(rows[2][1])["review_required"] is True
    assert rows[3] == ("[131]", json.dumps({"review_required": True}, separators=(",", ":")))
    assert rows[4] == ("[131]", AUTO)
    assert rows[5] == ("[131]", AUTO)
