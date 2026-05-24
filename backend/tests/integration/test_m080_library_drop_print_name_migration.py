"""m080 test — drop print_name from library_files.file_metadata (#1489)."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m080_library_drop_print_name as m080


@pytest.mark.asyncio
async def test_m080_drops_print_name_only():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE library_files (id INTEGER PRIMARY KEY, file_metadata TEXT)"))
        await conn.execute(
            text(
                "INSERT INTO library_files (id, file_metadata) VALUES "
                '(1, \'{"print_name": "Exported 3D Model", "layers": 42}\'), '
                "(2, '{\"layers\": 7}'), "
                "(3, NULL)"
            )
        )

        await m080.upgrade(conn)
        # Idempotent: a second run must not raise or change anything further.
        await m080.upgrade(conn)

        r1 = json.loads((await conn.execute(text("SELECT file_metadata FROM library_files WHERE id=1"))).scalar())
        assert "print_name" not in r1
        assert r1["layers"] == 42  # sibling keys preserved

        r2 = json.loads((await conn.execute(text("SELECT file_metadata FROM library_files WHERE id=2"))).scalar())
        assert r2 == {"layers": 7}  # untouched

        r3 = (await conn.execute(text("SELECT file_metadata FROM library_files WHERE id=3"))).scalar()
        assert r3 is None  # NULL metadata untouched
    await engine.dispose()
