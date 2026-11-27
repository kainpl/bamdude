"""m267 creates the table of the counts apps report on their sidebar links — once, empty."""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m267_external_link_badges as m267


@pytest.mark.asyncio
async def test_creates_the_badge_table_once(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/m267.db")
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE external_links (id INTEGER PRIMARY KEY, url VARCHAR(500))")
    async with engine.begin() as conn:
        await m267.upgrade(conn)
        await m267.upgrade(conn)  # a re-run (DEBUG=true) must not fail
        cols = {row[1] for row in (await conn.exec_driver_sql("PRAGMA table_info(external_link_badges)")).all()}
        rows = (await conn.exec_driver_sql("SELECT COUNT(*) FROM external_link_badges")).scalar_one()
    await engine.dispose()
    assert cols == {"link_id", "count", "updated_at"}
    assert rows == 0
    assert (m267.version, m267.name) == (267, "external_link_badges")
