"""m167 widens api_keys.key_hash / key_prefix on PostgreSQL and touches nothing on SQLite."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m167_api_key_column_widths as m167


def test_the_migration_declares_its_version_and_name():
    assert m167.version == 167
    assert m167.name == "api_key_column_widths"


async def test_sqlite_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(m167, "is_postgres", lambda: False)
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}")
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE api_keys (id INTEGER PRIMARY KEY, key_hash VARCHAR(64))"))
            await m167.upgrade(conn)
            ddl = (await conn.execute(text("SELECT sql FROM sqlite_master WHERE name='api_keys'"))).scalar()
        assert "VARCHAR(64)" in ddl  # untouched: SQLite never enforced the length anyway
    finally:
        await engine.dispose()


async def test_postgres_gets_both_columns_widened(monkeypatch):
    monkeypatch.setattr(m167, "is_postgres", lambda: True)
    statements: list[str] = []

    class Conn:
        async def execute(self, clause):
            statements.append(str(clause))

    await m167.upgrade(Conn())
    assert statements == [
        "ALTER TABLE api_keys ALTER COLUMN key_hash TYPE VARCHAR(255)",
        "ALTER TABLE api_keys ALTER COLUMN key_prefix TYPE VARCHAR(16)",
    ]
