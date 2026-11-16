"""Known historical NULLs are retained without retrying a failing ALTER."""

import pytest
from sqlalchemy import Column, DateTime, Integer, MetaData, Table, event, text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.db_portable import _reconcile_columns


async def test_import_conform_does_not_retry_a_declared_b_null_residual(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "residual.db"))
    metadata = MetaData()
    Table(
        "label_templates",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("created_at", DateTime, nullable=False),
    )
    attempts = []

    def record(_conn, _cursor, sql, _params, _context, _many):
        if sql.startswith("ALTER TABLE"):
            attempts.append(sql)

    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE label_templates(id INTEGER PRIMARY KEY, created_at DATETIME)"))
            await conn.execute(text("INSERT INTO label_templates VALUES(1,NULL),(2,'2026-01-02')"))
        event.listen(engine.sync_engine, "before_cursor_execute", record)
        await _reconcile_columns(engine, metadata)
        assert attempts == []
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT * FROM label_templates ORDER BY id"))).all() == [
                (1, None),
                (2, "2026-01-02"),
            ]
    finally:
        await engine.dispose()
