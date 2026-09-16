"""m176 creates the inbox table, its two indexes, the users column, and seeds the permission."""

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.migrations import m176_user_inbox as m176

_USERS = "CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(100))"


async def _names(conn, kind: str) -> set[str]:
    rows = await conn.execute(text(f"SELECT name FROM sqlite_master WHERE type = '{kind}'"))
    return {r[0] for r in rows.fetchall()}


async def _columns(conn, table: str) -> set[str]:
    rows = await conn.execute(text(f"PRAGMA table_info({table})"))
    return {r[1] for r in rows.fetchall()}


@pytest.mark.asyncio
async def test_upgrade_creates_table_indexes_and_column_and_is_idempotent(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/a.db")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_USERS)
        await m176.upgrade(conn)
        await m176.upgrade(conn)
        assert "user_notifications" in await _names(conn, "table")
        assert {"ix_user_notifications_user_read", "ix_user_notifications_user_created"} <= await _names(conn, "index")
        assert "inbox_events" in await _columns(conn, "users")
        assert {
            "id",
            "user_id",
            "event_type",
            "severity",
            "title",
            "message",
            "printer_id",
            "printer_name",
            "extra_data",
            "created_at",
            "read_at",
        } <= await _columns(conn, "user_notifications")
    await engine.dispose()


@pytest.mark.asyncio
async def test_seed_grants_the_permission_to_the_three_system_groups_once(test_engine):
    from backend.app.migrations.m001_bamdude_baseline import _seed_default_groups
    from backend.app.models.group import Group

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    await _seed_default_groups(factory)
    async with factory() as db:
        for row in (await db.execute(select(Group.id, Group.permissions))).all():
            stripped = [p for p in (row.permissions or []) if p != "notifications:inbox"]
            await db.execute(update(Group).where(Group.id == row.id).values(permissions=stripped))
        await db.commit()

    await m176.seed(factory)
    await m176.seed(factory)

    async with factory() as db:
        rows = (await db.execute(select(Group.name, Group.permissions))).all()
    by_name = {r.name: r.permissions for r in rows}
    for name in ("Administrators", "Operators", "Viewers"):
        assert by_name[name].count("notifications:inbox") == 1, name


class _EmptyResult:
    """Answers every probe with "nothing is there yet"."""

    def scalar(self):
        return None

    def fetchall(self):
        return []


class _Recorder:
    """A connection that records the SQL a migration sends instead of running it.

    The PostgreSQL branch of ``m176.upgrade`` has no other coverage:
    ``test_migration_ddl_dialects.py`` judges only ``CREATE TABLE`` statements
    whose table is absent from ``Base.metadata``, and ``user_notifications`` is
    modelled, so that scan filters m176 out before it looks at a single token.
    The branch is live all the same — the SQLite→PostgreSQL move imports the
    file at its own migration level and the chain resumes at N+1 against a
    reflected schema that has no inbox table.
    """

    def __init__(self):
        self.statements: list[str] = []

    async def exec_driver_sql(self, sql):
        self.statements.append(str(sql))
        return _EmptyResult()

    async def execute(self, clause, params=None):
        self.statements.append(str(clause))
        return _EmptyResult()


@pytest.mark.asyncio
async def test_the_postgresql_branch_speaks_postgresql(monkeypatch):
    monkeypatch.setattr("backend.app.core.db_dialect.is_postgres", lambda: True)
    monkeypatch.setattr("backend.app.core.db_dialect.is_sqlite", lambda: False)
    monkeypatch.setattr("backend.app.migrations.m176_user_inbox.is_sqlite", lambda: False)
    monkeypatch.setattr("backend.app.migrations.helpers.is_postgres", lambda: True)

    conn = _Recorder()
    await m176.upgrade(conn)
    sql = "\n".join(conn.statements)

    create = next(s for s in conn.statements if "CREATE TABLE user_notifications" in s)
    assert "SERIAL PRIMARY KEY" in create
    assert "AUTOINCREMENT" not in sql  # SQLite-only spelling; PostgreSQL rejects it
    assert "DATETIME" not in create  # PostgreSQL wants TIMESTAMP
    assert create.count("TIMESTAMP") == 2  # created_at and read_at
    assert "REFERENCES users(id) ON DELETE CASCADE" in create

    # Both indexes are sent outside the table guard, so a fresh install that got
    # the table from ``create_all`` still gets them.
    assert sum("CREATE INDEX IF NOT EXISTS" in s for s in conn.statements) == 2

    # And the users column is added with a PostgreSQL-legal type.
    add_column = next(s for s in conn.statements if "ALTER TABLE users ADD COLUMN inbox_events" in s)
    assert "JSON" in add_column
