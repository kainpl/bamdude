"""m251 marks the history's prints whose plate the swap changed by itself — and nothing else."""

import json

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m251_swap_print_outcome_checked as m251

FINISHED = json.dumps({"bamdude_terminal_effects": {"swap_change_table": {"stage": "finished", "owner": "live"}}})


def _effect(stage: str) -> str:
    return json.dumps({"bamdude_terminal_effects": {"swap_change_table": {"stage": stage}}})


def test_identity():
    assert m251.version == 251
    assert m251.name == "swap_print_outcome_checked"


async def _marks(engine) -> dict[int, str | None]:
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT id, outcome_checked_at FROM print_archives"))).all()
    return {row[0]: row[1] for row in rows}


@pytest.mark.asyncio
async def test_only_completed_unmarked_prints_with_a_finished_swap_are_marked(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm251.db'}")
    rows = [
        # id, status, completed_at, created_at, deleted_at, is_calibration, outcome_checked_at, extra_data
        (1, "completed", "2026-11-10 10:00:00.000000", "2026-11-10 08:00:00.000000", None, 0, None, FINISHED),
        (2, "completed", "2026-11-10 11:00:00.000000", None, None, 0, None, _effect("failed")),
        (3, "completed", "2026-11-10 12:00:00.000000", None, None, 0, None, _effect("settled_by_operator")),
        (4, "completed", "2026-11-10 13:00:00.000000", None, None, 0, None, None),
        (5, "completed", "2026-11-10 14:00:00.000000", None, "2026-11-11 09:00:00.000000", 0, None, FINISHED),
        (6, "completed", "2026-11-10 15:00:00.000000", None, None, 1, None, FINISHED),
        (7, "completed", "2026-11-10 16:00:00.000000", None, None, 0, "2026-11-10 16:05:00.000000", FINISHED),
        (8, "failed", "2026-11-10 17:00:00.000000", None, None, 0, None, FINISHED),
        (9, "completed", "2026-11-10 18:00:00.000000", None, None, 0, None, "{not json"),
        (10, "completed", None, "2026-11-10 07:00:00.000000", None, None, None, FINISHED),
    ]
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE print_archives (id INTEGER PRIMARY KEY, status VARCHAR(20), completed_at DATETIME, "
                "created_at DATETIME, deleted_at DATETIME, is_calibration BOOLEAN, outcome_checked_at DATETIME, "
                "extra_data JSON)"
            )
        )
        for row in rows:
            await conn.execute(
                text("INSERT INTO print_archives VALUES (:i, :s, :c, :m, :d, :k, :o, :e)"),
                dict(zip(("i", "s", "c", "m", "d", "k", "o", "e"), row, strict=True)),
            )
        await m251.upgrade(conn)

    marks = await _marks(engine)
    assert marks[1] == "2026-11-10 10:00:00.000000", "a finished swap is marked at the print's own end"
    assert marks[10] == "2026-11-10 07:00:00.000000", "else at its creation"
    assert marks[7] == "2026-11-10 16:05:00.000000", "an earlier mark stays"
    for untouched in (2, 3, 4, 5, 6, 8, 9):
        assert marks[untouched] is None, untouched

    # DEBUG re-runs the latest migration: nothing the rule would not mark.
    async with engine.begin() as conn:
        await m251.upgrade(conn)
    assert await _marks(engine) == marks
    await engine.dispose()
