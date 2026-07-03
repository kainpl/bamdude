"""m087 test — sync virtual-printer access codes from their target printer.

Non-proxy VPs with a target printer forward the slicer's auth bytes through the
live-mirror bridge to the real printer, so their ``access_code`` MUST equal the
target's. The migration backfills any pre-existing diverged rows via a
correlated subquery; proxy VPs and standalone (no-target) VPs are left alone.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m087_vp_access_code_sync as m087


@pytest.mark.asyncio
async def test_m087_syncs_only_diverged_non_proxy_targets():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE printers (id INTEGER PRIMARY KEY, name TEXT, access_code TEXT)"))
        await conn.execute(
            text(
                "CREATE TABLE virtual_printers "
                "(id INTEGER PRIMARY KEY, name TEXT, mode TEXT, access_code TEXT, target_printer_id INTEGER)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO printers (id, name, access_code) VALUES "
                "(1, 'Real A', 'AAAAAAAA'), "
                "(2, 'Real B', 'BBBBBBBB')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO virtual_printers (id, name, mode, access_code, target_printer_id) VALUES "
                "(1, 'diverged',   'print_queue',  'WRONGONE', 1), "  # non-proxy mismatch → sync to AAAAAAAA
                "(2, 'null-code',  'file_manager', NULL,       2), "  # non-proxy NULL → sync to BBBBBBBB
                "(3, 'proxy-keep', 'proxy',        'WRONGTWO', 1), "  # proxy → LEFT untouched
                "(4, 'already-ok', 'print_queue',  'AAAAAAAA', 1), "  # already matches → untouched
                "(5, 'standalone', 'file_manager', 'OWNCODE1', NULL)"  # no target → untouched
            )
        )

        await m087.upgrade(conn)
        # Idempotent: re-running must not change anything further.
        await m087.upgrade(conn)

        async def code(vp_id: int) -> str | None:
            return (
                await conn.execute(text("SELECT access_code FROM virtual_printers WHERE id=:i"), {"i": vp_id})
            ).scalar()

        assert await code(1) == "AAAAAAAA"  # diverged non-proxy → synced
        assert await code(2) == "BBBBBBBB"  # NULL non-proxy → synced
        assert await code(3) == "WRONGTWO"  # proxy → untouched
        assert await code(4) == "AAAAAAAA"  # already matching → untouched
        assert await code(5) == "OWNCODE1"  # standalone → untouched

    await engine.dispose()
