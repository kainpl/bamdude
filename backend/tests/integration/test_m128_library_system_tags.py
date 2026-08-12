"""m128 — system tags become catalog rows, associations backfilled.

The DDL below is written by hand as ``library_tags`` looked BEFORE this
migration. It must NOT be derived from today's models: a prepared database that
already has the new columns makes ``add_column`` a no-op and every assertion
here passes without the migration doing anything at all.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.migrations import m128_library_system_tags as m128

PRE_M128_DDL = """
CREATE TABLE library_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(64) NOT NULL,
    name_key VARCHAR(64) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX ix_library_tags_name_key ON library_tags (name_key);
CREATE TABLE library_file_tags (
    file_id INTEGER NOT NULL,
    tag_id INTEGER NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (file_id, tag_id)
);
CREATE TABLE library_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename VARCHAR(255) NOT NULL,
    file_path VARCHAR(500) NOT NULL,
    file_size INTEGER NOT NULL,
    file_type VARCHAR(50) NOT NULL,
    file_tags TEXT NOT NULL DEFAULT '[]',
    deleted_at DATETIME
);
"""


@pytest_asyncio.fixture
async def engine(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm128.db'}")
    async with eng.begin() as conn:
        for statement in PRE_M128_DDL.strip().split(";"):
            if statement.strip():
                await conn.execute(text(statement))
    yield eng
    await eng.dispose()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_prepared_database_really_lacks_the_columns(engine):
    """A guard on the guard. If the fixture ever grows the new columns, every
    other test in this file passes while the migration does nothing."""
    async with engine.begin() as conn:
        cols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(library_tags)"))).all()}

    assert "is_system" not in cols
    assert "code" not in cols


@pytest.mark.asyncio
@pytest.mark.integration
async def test_upgrade_adds_the_columns_and_swaps_the_index(engine):
    async with engine.begin() as conn:
        await m128.upgrade(conn)

    async with engine.begin() as conn:
        cols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(library_tags)"))).all()}
        indexes = {r[1] for r in (await conn.execute(text("PRAGMA index_list(library_tags)"))).all()}

    assert {"is_system", "code"} <= cols
    assert "ix_library_tags_name_key_is_system" in indexes
    assert "ix_library_tags_code" in indexes
    # The single-column unique index has to GO, or a user tag named after a
    # system one still collides and the composite index is decoration.
    assert "ix_library_tags_name_key" not in indexes


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_user_tag_may_keep_a_name_a_system_tag_also_uses(engine):
    """The whole point of the composite index, asserted against the database
    the migration actually produced rather than against the model."""
    async with engine.begin() as conn:
        await m128.upgrade(conn)
        await conn.execute(
            text("INSERT INTO library_tags (name, name_key, is_system, code) VALUES ('sliced', 'sliced', 0, NULL)")
        )
        await conn.execute(
            text("INSERT INTO library_tags (name, name_key, is_system, code) VALUES ('SLICED', 'sliced', 1, 'sliced')")
        )
        count = (await conn.execute(text("SELECT COUNT(*) FROM library_tags WHERE name_key = 'sliced'"))).scalar_one()

    assert count == 2


@pytest.mark.asyncio
@pytest.mark.integration
async def test_upgrade_is_idempotent(engine):
    """DEBUG=true re-runs the latest migration on startup."""
    async with engine.begin() as conn:
        await m128.upgrade(conn)
    async with engine.begin() as conn:
        await m128.upgrade(conn)


@pytest_asyncio.fixture
async def seeded(engine):
    """Upgrade, insert files with known file_tags, then seed."""
    async with engine.begin() as conn:
        await m128.upgrade(conn)
        await conn.execute(
            text(
                "INSERT INTO library_files (filename, file_path, file_size, file_type, file_tags) VALUES "
                "('a.gcode.3mf', '/tmp/a', 1, 'gcode', '[\"gcode\", \"3mf\", \"sliced\"]'), "
                "('b.stl', '/tmp/b', 1, 'stl', '[\"stl\", \"geometry\"]')"
            )
        )
        # A trashed file is still in the library and can be restored; skipping
        # it would leave a restored file invisible to every filter.
        await conn.execute(
            text(
                "INSERT INTO library_files (filename, file_path, file_size, file_type, file_tags, deleted_at) "
                "VALUES ('c.stl', '/tmp/c', 1, 'stl', '[\"stl\"]', CURRENT_TIMESTAMP)"
            )
        )
    await m128.seed(async_sessionmaker(engine, expire_on_commit=False))
    return engine


@pytest.mark.asyncio
@pytest.mark.integration
async def test_seed_inserts_every_system_tag_once(seeded):
    async with seeded.begin() as conn:
        rows = (await conn.execute(text("SELECT code, is_system FROM library_tags"))).all()

    codes = [r[0] for r in rows]
    assert sorted(codes) == sorted(c for c, _ in m128.SYSTEM_TAGS)
    assert len(codes) == len(set(codes))
    assert all(r[1] for r in rows)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_seed_does_not_duplicate_on_a_second_run(seeded):
    await m128.seed(async_sessionmaker(seeded, expire_on_commit=False))

    async with seeded.begin() as conn:
        count = (await conn.execute(text("SELECT COUNT(*) FROM library_tags"))).scalar_one()
        assoc = (await conn.execute(text("SELECT COUNT(*) FROM library_file_tags"))).scalar_one()

    assert count == len(m128.SYSTEM_TAGS)
    assert assoc == 6  # 3 + 2 + 1, unchanged


@pytest.mark.asyncio
@pytest.mark.integration
async def test_seed_backfills_associations_from_file_tags(seeded):
    async with seeded.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT f.filename, t.code FROM library_file_tags ft "
                    "JOIN library_files f ON f.id = ft.file_id "
                    "JOIN library_tags t ON t.id = ft.tag_id"
                )
            )
        ).all()

    by_file: dict[str, set[str]] = {}
    for filename, code in rows:
        by_file.setdefault(filename, set()).add(code)

    assert by_file["a.gcode.3mf"] == {"gcode", "3mf", "sliced"}
    assert by_file["b.stl"] == {"stl", "geometry"}
    assert by_file["c.stl"] == {"stl"}  # trashed, still backfilled
