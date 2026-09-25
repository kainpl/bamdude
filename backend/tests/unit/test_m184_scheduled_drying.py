"""m184 creates the two scheduled-drying tables on an existing database."""

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m184_scheduled_drying as m184


@pytest.mark.asyncio
async def test_m184_creates_both_tables(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    async with engine.begin() as conn:
        await m184.upgrade(conn)
        await m184.upgrade(conn)  # idempotent

        def _cols(sync_conn):
            insp = inspect(sync_conn)
            return (
                {c["name"] for c in insp.get_columns("drying_schedules")},
                {c["name"] for c in insp.get_columns("scheduled_dryings")},
            )

        schedules, runs = await conn.run_sync(_cols)
    await engine.dispose()
    assert {"printer_id", "ams_id", "start_time", "weekdays", "latest_start", "enabled"} <= schedules
    assert {"schedule_id", "start_after", "latest_start", "status", "reason", "detail", "started_at"} <= runs


def test_the_models_match_the_migration_columns():
    from backend.app.models.scheduled_drying import DryingSchedule, ScheduledDrying

    assert {"start_time", "weekdays", "latest_start", "enabled"} <= set(DryingSchedule.__table__.c.keys())
    assert {"schedule_id", "reason", "detail", "latest_start"} <= set(ScheduledDrying.__table__.c.keys())
