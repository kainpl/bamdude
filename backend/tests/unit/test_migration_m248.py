"""m248 adds the «result checked» columns and stamps the prints that already exist — once."""

from datetime import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m248_archive_outcome_checked as m248


def test_identity():
    assert m248.version == 248
    assert m248.name == "archive_outcome_checked"


async def _stored(engine) -> dict[int, tuple]:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(text("SELECT id, outcome_checked_at, outcome_reminded_at FROM print_archives"))
        ).all()
    return {row[0]: (row[1], row[2]) for row in rows}


@pytest.mark.asyncio
async def test_existing_prints_are_stamped_and_a_second_run_stamps_nothing_new(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm248.db'}")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE print_archives (id INTEGER PRIMARY KEY, status VARCHAR(20), "
                "completed_at DATETIME, created_at DATETIME)"
            )
        )
        for row in (
            {
                "id": 1,
                "status": "completed",
                "done": "2026-11-01 10:00:00.000000",
                "made": "2026-11-01 09:00:00.000000",
            },
            {"id": 2, "status": "failed", "done": None, "made": "2026-11-02 09:00:00.000000"},
            {"id": 3, "status": "completed", "done": None, "made": None},
            {"id": 4, "status": "printing", "done": None, "made": "2026-11-03 09:00:00.000000"},
        ):
            await conn.execute(
                text(
                    "INSERT INTO print_archives (id, status, completed_at, created_at) "
                    "VALUES (:id, :status, :done, :made)"
                ),
                row,
            )
        await m248.upgrade(conn)

    first = await _stored(engine)
    assert first[1] == ("2026-11-01 10:00:00.000000", None), "its own end"
    assert first[2] == ("2026-11-02 09:00:00.000000", None), "else its creation"
    assert first[3][0] is not None and first[3][1] is None, "else now"
    assert first[4] == (None, None), "a print still running at the upgrade is new work"
    assert datetime.fromisoformat(first[3][0]).year >= 2026

    # A print that finishes after the upgrade, then a repeated run (DEBUG re-runs the latest migration).
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO print_archives (id, status, completed_at) VALUES (5, 'completed', NULL)"))
        await conn.execute(text("UPDATE print_archives SET status = 'completed' WHERE id = 4"))
        await m248.upgrade(conn)

    second = await _stored(engine)
    assert second[5] == (None, None) and second[4] == (None, None), "only the run that adds the column stamps"
    assert {k: v for k, v in second.items() if k in (1, 2, 3)} == {k: v for k, v in first.items() if k in (1, 2, 3)}

    async with engine.connect() as conn:
        indexes = {row[1] for row in (await conn.execute(text("PRAGMA index_list('print_archives')"))).all()}
    assert "ix_print_archives_outcome_checked_at" in indexes
    await engine.dispose()
