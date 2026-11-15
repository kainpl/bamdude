"""Real SQLite DDL and data proofs for the frozen index repair."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core import schema_index_repair as repair
from backend.app.core.schema_repair_state import RESIDUALS, SchemaRepairPending, read_record


@pytest.fixture
async def conn(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'indexes.db'}")
    async with engine.begin() as connection:
        for row in repair.manifest():
            columns = set(row["columns"])
            if row["name"] == "ix_print_archives_effective_hash":
                columns.add("content_hash")
            if row["predicate"]:
                columns.add("is_active" if row["name"] == repair.CALIBRATION else "deleted_at")
            existing = (await connection.execute(text(f'PRAGMA table_info("{row["table"]}")'))).all()
            if existing:
                present = {r.name for r in existing}
                for col in columns - present:
                    await connection.execute(text(f'ALTER TABLE "{row["table"]}" ADD COLUMN "{col}"'))
            else:
                names = ",".join('"' + c + '"' for c in columns)
                await connection.execute(text(f'CREATE TABLE "{row["table"]}"({names})'))
        yield connection
    await engine.dispose()


async def test_frozen_manifest_restores_every_index_and_replay_is_read_only(conn):
    assert await repair.repair_indexes(conn) == []
    before = await conn.scalar(text("PRAGMA schema_version"))
    assert await repair.repair_indexes(conn) == []
    assert await conn.scalar(text("PRAGMA schema_version")) == before
    rows = (await conn.execute(text("SELECT name FROM sqlite_schema WHERE type='index'"))).scalars().all()
    assert set(rows) == {r["name"] for r in repair.manifest()}


async def test_calibration_residual_survives_reopen_then_domain_correction_enforces_it(conn):
    await conn.execute(text("INSERT INTO filament_calibration VALUES (1,1,1,1,1,1),(1,1,1,1,1,1)"))
    # All six values are 1 regardless of fixture column ordering.
    await repair.native_upgrade(conn)
    saved = await read_record(conn, RESIDUALS)
    entry = saved["entries"]["filament_calibration.indexes." + repair.CALIBRATION]
    assert entry["reason"] == "duplicate_identity_groups" and entry["count"] == 1
    assert await conn.scalar(text("SELECT count(*) FROM filament_calibration")) == 2
    await conn.execute(text("UPDATE filament_calibration SET is_active=0 WHERE rowid=2"))
    await repair.retry_index_residuals(conn)
    assert (await read_record(conn, RESIDUALS))["entries"] == {}
    assert await conn.scalar(text("SELECT count(*) FROM sqlite_schema WHERE name='ux_filament_cali_active'")) == 1
    async with conn.begin_nested():
        with pytest.raises(Exception, match="UNIQUE constraint failed"):
            await conn.execute(text("INSERT INTO filament_calibration VALUES (1,1,1,1,1,1)"))


async def test_restore_cannot_silently_accept_calibration_duplicates(conn):
    await conn.execute(text("INSERT INTO filament_calibration VALUES (1,1,1,1,1,1),(1,1,1,1,1,1)"))
    with pytest.raises(SchemaRepairPending, match="calibration_confirmation_required"):
        await repair.repair_indexes(conn)
    assert len(await repair.repair_indexes(conn, accepted_calibration=True)) == 1


@pytest.mark.parametrize("variant", ["desc", "collation", "unique", "partial"])
async def test_unknown_same_name_shape_is_never_replaced(conn, variant):
    sql = {
        "desc": "CREATE INDEX ix_auto_queue_batch_id ON auto_queue_items(batch_id DESC)",
        "collation": "CREATE INDEX ix_auto_queue_batch_id ON auto_queue_items(batch_id COLLATE NOCASE)",
        "unique": "CREATE UNIQUE INDEX ix_auto_queue_batch_id ON auto_queue_items(batch_id)",
        "partial": "CREATE INDEX ix_auto_queue_batch_id ON auto_queue_items(batch_id) WHERE batch_id IS NOT NULL",
    }[variant]
    await conn.execute(text(sql))
    with pytest.raises(SchemaRepairPending, match="unknown_index_shape"):
        await repair.repair_indexes(conn)
    result = await repair.repair_indexes(conn, native=True)
    assert any(r["object"] == "auto_queue_items.indexes.ix_auto_queue_batch_id" for r in result)
    assert await conn.scalar(text("SELECT sql FROM sqlite_schema WHERE name='ix_auto_queue_batch_id'")) == sql


async def test_old_expression_sql_is_recognised_but_different_expression_is_not(conn):
    await conn.execute(
        text(
            "CREATE INDEX ix_print_archives_effective_hash ON print_archives "
            "((COALESCE(source_content_hash, content_hash)))"
        )
    )
    assert await repair.repair_indexes(conn) == []
    await conn.execute(text("DROP INDEX ix_print_archives_effective_hash"))
    await conn.execute(text("CREATE INDEX ix_print_archives_effective_hash ON print_archives(lower(content_hash))"))
    with pytest.raises(SchemaRepairPending, match="unknown_index_shape"):
        await repair.repair_indexes(conn)
