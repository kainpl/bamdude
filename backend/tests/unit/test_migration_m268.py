"""m268 gives library folders an owner and a ``shared`` flag, and settles the existing ones once (N56 D2).

A folder whose files — all the way down, trashed ones too — belong to one user becomes that user's,
and an empty folder inside it goes with it. Every other folder is shared, so it stays visible as
before: empty, several uploaders, ownerless files, external, bound to a product, a Virtual Printer's
target, and the root ``MakerWorld`` / ``Manyfold`` / ``Telegram`` folders. The backfill runs only
when ``shared`` was added just now; ``replay`` (a DEBUG start) adds columns and touches no row.
"""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m268_library_folder_owners as m268

FOLDERS = [
    # id, parent, external, name
    (1, None, 0, "alice root"),  # alice's files only, a trashed one among them
    (2, 1, 0, "alice empty child"),  # empty, inside alice's → alice's
    (3, 1, 0, "alice child"),  # alice's file
    (4, None, 0, "mixed"),  # alice + bob
    (5, None, 0, "empty root"),  # empty at the root → shared
    (6, None, 0, "ownerless"),  # a file without an owner → shared
    (7, None, 1, "nas"),  # external → shared
    (8, None, 0, "bound"),  # bob's files, bound to a product → shared
    (9, None, 0, "vp target"),  # bob's files, a VP's target → shared
    (10, None, 0, "MakerWorld"),  # bob's files, the import root → shared
    (11, 4, 0, "Telegram"),  # bob's files; "Telegram" only counts at the root → bob's
    (12, None, 0, "Telegram"),  # empty at the root → shared
]
FILES = [
    # folder, owner, trashed
    (1, 1, 0),
    (1, 1, 1),
    (3, 1, 0),
    (4, 1, 0),
    (4, 2, 0),
    (6, None, 0),
    (7, None, 0),
    (8, 2, 0),
    (9, 2, 0),
    (10, 2, 0),
    (11, 2, 0),
]


async def _legacy_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/m268.db")
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY)")
        await conn.exec_driver_sql(
            "CREATE TABLE library_folders (id INTEGER PRIMARY KEY, parent_id INTEGER, "
            "is_external BOOLEAN NOT NULL DEFAULT 0, name VARCHAR(255))"
        )
        await conn.exec_driver_sql(
            "CREATE TABLE library_files (id INTEGER PRIMARY KEY, folder_id INTEGER, created_by_id INTEGER, "
            "deleted_at DATETIME)"
        )
        await conn.exec_driver_sql("CREATE TABLE product_folders (product_id INTEGER, library_folder_id INTEGER)")
        await conn.exec_driver_sql("CREATE TABLE virtual_printers (id INTEGER PRIMARY KEY, target_folder_id INTEGER)")
        await conn.exec_driver_sql("INSERT INTO users (id) VALUES (1), (2)")
        for fid, parent, ext, name in FOLDERS:
            await conn.exec_driver_sql(
                "INSERT INTO library_folders (id, parent_id, is_external, name) VALUES (?, ?, ?, ?)",
                (fid, parent, ext, name),
            )
        for folder, owner, trashed in FILES:
            await conn.exec_driver_sql(
                "INSERT INTO library_files (folder_id, created_by_id, deleted_at) VALUES (?, ?, ?)",
                (folder, owner, "2026-01-01 00:00:00" if trashed else None),
            )
        await conn.exec_driver_sql("INSERT INTO product_folders VALUES (1, 8)")
        await conn.exec_driver_sql("INSERT INTO virtual_printers (id, target_folder_id) VALUES (1, 9), (2, NULL)")
    return engine


async def _settled(conn) -> dict[int, tuple]:
    rows = await conn.exec_driver_sql("SELECT id, created_by_id, shared FROM library_folders ORDER BY id")
    return {fid: (owner, bool(shared)) for fid, owner, shared in rows.all()}


@pytest.mark.asyncio
async def test_settles_the_existing_folders_as_upstream_with_our_always_shared(tmp_path):
    engine = await _legacy_db(tmp_path)
    async with engine.begin() as conn:
        await m268.upgrade(conn)
        settled = await _settled(conn)
    await engine.dispose()

    assert settled == {
        1: (1, False),
        2: (1, False),
        3: (1, False),
        4: (None, True),
        5: (None, True),
        6: (None, True),
        7: (None, True),
        8: (None, True),
        9: (None, True),
        10: (None, True),
        11: (2, False),
        12: (None, True),
    }
    assert (m268.version, m268.name) == (268, "library_folder_owners")


@pytest.mark.asyncio
async def test_a_rerun_and_a_replay_never_undo_an_admins_later_choice(tmp_path):
    engine = await _legacy_db(tmp_path)
    async with engine.begin() as conn:
        await m268.upgrade(conn)
        # The admin makes alice's folder shared and the empty root private afterwards.
        await conn.exec_driver_sql("UPDATE library_folders SET shared = 1 WHERE id = 1")
        await conn.exec_driver_sql("UPDATE library_folders SET shared = 0 WHERE id = 5")
        before = await _settled(conn)
    async with engine.begin() as conn:
        await m268.upgrade(conn)  # the columns exist: no backfill
        await m268.replay(conn)  # DEBUG start: columns only
        after = await _settled(conn)
    await engine.dispose()

    assert after == before
    assert after[1] == (1, True) and after[5] == (None, False)


@pytest.mark.asyncio
async def test_a_parent_loop_does_not_hang_the_start(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/loop.db")
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY)")
        await conn.exec_driver_sql(
            "CREATE TABLE library_folders (id INTEGER PRIMARY KEY, parent_id INTEGER, "
            "is_external BOOLEAN NOT NULL DEFAULT 0, name VARCHAR(255))"
        )
        await conn.exec_driver_sql(
            "CREATE TABLE library_files (id INTEGER PRIMARY KEY, folder_id INTEGER, created_by_id INTEGER)"
        )
        await conn.exec_driver_sql("INSERT INTO users (id) VALUES (1)")
        await conn.exec_driver_sql("INSERT INTO library_folders VALUES (1, 2, 0, 'a'), (2, 1, 0, 'b')")
        await conn.exec_driver_sql("INSERT INTO library_files (folder_id, created_by_id) VALUES (1, 1)")
    async with engine.begin() as conn:
        await m268.upgrade(conn)
        settled = await _settled(conn)
    await engine.dispose()

    assert set(settled) == {1, 2}
