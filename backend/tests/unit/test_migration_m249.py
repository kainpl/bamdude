"""m249 gives the default Operators group ``archives:reprint_all`` — that group only, once."""

import json

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.core.permissions import DEFAULT_GROUPS, Permission
from backend.app.migrations import m249_operators_reprint_all as m249

GRANT = "archives:reprint_all"


def test_identity():
    assert m249.version == 249
    assert m249.name == "operators_reprint_all"
    granted = m249.NEW_PERMISSIONS
    assert granted == [Permission.ARCHIVES_REPRINT_ALL.value]


def test_a_fresh_install_gives_operators_the_same_grant_and_widens_nothing_else():
    operators = set(DEFAULT_GROUPS["Operators"]["permissions"])
    assert {Permission.ARCHIVES_REPRINT_ALL.value, Permission.ARCHIVES_REPRINT_OWN.value} <= operators
    # The gate also asks for read; operators already read farm-wide, and nothing else was opened.
    assert Permission.ARCHIVES_READ_ALL.value in operators
    assert Permission.ARCHIVES_UPDATE_ALL.value not in operators
    assert Permission.ARCHIVES_DELETE_ALL.value not in operators
    assert Permission.ARCHIVES_REPRINT_ALL.value not in DEFAULT_GROUPS["Viewers"]["permissions"]


async def _stored(factory) -> dict[int, list[str]]:
    async with factory() as db:
        rows = (await db.execute(text("SELECT id, permissions FROM groups"))).all()
    return {row[0]: json.loads(row[1]) if row[1] is not None else None for row in rows}


@pytest.mark.asyncio
async def test_only_the_system_operators_group_gets_it_and_a_second_run_changes_nothing(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm249.db'}")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    own = ["archives:read_all", "archives:reprint_own"]
    groups = {
        1: ("Administrators", 1, ["archives:reprint_all", "settings:update"]),
        2: ("Operators", 1, own),
        3: ("Viewers", 1, ["archives:read_all"]),
        4: ("Night shift", 0, own),  # a custom group
        5: ("Operators (old)", 1, own),  # the system group, renamed
        6: ("Empty", 0, None),
    }
    async with engine.begin() as conn:
        await conn.execute(
            text("CREATE TABLE groups (id INTEGER PRIMARY KEY, name VARCHAR(100), is_system BOOLEAN, permissions JSON)")
        )
        for group_id, (group_name, system, permissions) in groups.items():
            await conn.execute(
                text("INSERT INTO groups (id, name, is_system, permissions) VALUES (:id, :name, :system, :perms)"),
                {
                    "id": group_id,
                    "name": group_name,
                    "system": system,
                    "perms": None if permissions is None else json.dumps(permissions),
                },
            )
        await m249.upgrade(conn)

    await m249.seed(factory)
    first = await _stored(factory)
    assert first[2] == own + [GRANT], "appended, the rest kept in order"
    assert {k: v for k, v in first.items() if k != 2} == {k: v[2] for k, v in groups.items() if k != 2}

    await m249.seed(factory)
    assert await _stored(factory) == first
    await engine.dispose()


@pytest.mark.asyncio
async def test_a_deleted_operators_group_is_not_brought_back(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm249-none.db'}")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.execute(
            text("CREATE TABLE groups (id INTEGER PRIMARY KEY, name VARCHAR(100), is_system BOOLEAN, permissions JSON)")
        )
        # A custom group somebody named Operators after deleting the system one.
        await conn.execute(
            text("INSERT INTO groups (id, name, is_system, permissions) VALUES (1, 'Operators', 0, '[]')")
        )

    await m249.seed(factory)

    assert await _stored(factory) == {1: []}
    await engine.dispose()
