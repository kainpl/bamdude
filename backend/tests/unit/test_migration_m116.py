"""m116 returns ``require_previous_success`` to the per-printer queue."""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m116_require_previous_success


async def _upgraded(tmp_path, name: str) -> set[str]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/{name}")
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE print_queue (id INTEGER PRIMARY KEY, queue_id INTEGER)")
        await m116_require_previous_success.upgrade(conn)
        rows = await conn.exec_driver_sql("PRAGMA table_info(print_queue)")
        columns = {r[1] for r in rows.fetchall()}
    await engine.dispose()
    return columns


@pytest.mark.asyncio
async def test_m116_adds_columns_to_existing_table(tmp_path):
    assert {"require_previous_success", "gate_acknowledged"} <= await _upgraded(tmp_path, "t.db")


@pytest.mark.asyncio
async def test_m116_is_idempotent(tmp_path):
    """Re-running must not raise — DEBUG=true re-runs the latest migration."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t2.db")
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE print_queue (id INTEGER PRIMARY KEY, queue_id INTEGER)")
        await m116_require_previous_success.upgrade(conn)
        await m116_require_previous_success.upgrade(conn)
    await engine.dispose()


@pytest.mark.asyncio
async def test_both_columns_default_to_off(tmp_path):
    """An existing queue must not start gating work the user queued under the
    old behaviour, and no past failure may arrive pre-acknowledged."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t3.db")
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE print_queue (id INTEGER PRIMARY KEY, queue_id INTEGER)")
        await m116_require_previous_success.upgrade(conn)
        await conn.exec_driver_sql("INSERT INTO print_queue (queue_id) VALUES (1)")
        row = (
            await conn.exec_driver_sql("SELECT require_previous_success, gate_acknowledged FROM print_queue")
        ).fetchone()
    await engine.dispose()

    assert row == (0, 0)


def test_m116_version_and_name():
    assert m116_require_previous_success.version == 116
    assert m116_require_previous_success.name == "require_previous_success"


def test_115_belongs_to_the_zigbee_branch():
    """m116 skipped 115 so the zigbee branch could keep it, and the branches have
    now met: the guard flips from "no m115 here" to "exactly one m115, and it is
    the zigbee one". Two files claiming version 115 would make the applied-set
    ambiguous, which is what the gap was reserved to prevent."""
    from pathlib import Path

    # Located from the migration package itself, not from the repo root: CI runs
    # pytest with backend/ as the working directory, where a root-relative path
    # finds an empty folder and the guard silently passes on zero files.
    migrations = Path(m116_require_previous_success.__file__).resolve().parent
    found = sorted(p.name for p in migrations.glob("m115_*.py"))
    assert found == ["m115_zigbee_plug.py"], f"m115 is the zigbee migration's slot, found: {found}"


def test_model_declares_the_columns_for_fresh_installs():
    """The second of the two places a column must exist (CLAUDE.md): fresh
    installs build their schema from ``Base.metadata.create_all``, never from
    the migration chain."""
    from backend.app.models.print_queue import PrintQueueItem

    columns = set(PrintQueueItem.__table__.columns.keys())
    assert {"require_previous_success", "gate_acknowledged"} <= columns
