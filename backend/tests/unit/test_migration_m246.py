"""m246 adds the wear rate and the two archive columns, empty, and is safe to run twice."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import helpers, m246_wear_cost as m246


def test_the_migration_declares_its_version_and_name():
    assert m246.version == 246
    assert m246.name == "wear_cost"


async def _columns(conn, table: str) -> dict[str, tuple[str, int]]:
    rows = await conn.execute(text(f"PRAGMA table_info({table})"))
    return {r[1]: (r[2].upper(), r[3]) for r in rows}


async def test_upgrade_adds_nullable_columns_and_keeps_existing_rows_empty(tmp_path, monkeypatch):
    # ``is_sqlite()`` reads the app's configured DATABASE_URL, not this engine -- pin it.
    monkeypatch.setattr(helpers, "is_sqlite", lambda: True, raising=False)

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm246.db'}")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE printers (id INTEGER PRIMARY KEY, name TEXT)"))
        await conn.execute(text("CREATE TABLE print_archives (id INTEGER PRIMARY KEY, status TEXT)"))
        await conn.execute(text("INSERT INTO printers (id, name) VALUES (1, 'P')"))
        await conn.execute(text("INSERT INTO print_archives (id, status) VALUES (1, 'completed')"))

        await m246.upgrade(conn)
        await m246.upgrade(conn)  # second run: the column guard holds

        printers = await _columns(conn, "printers")
        archives = await _columns(conn, "print_archives")
        rate = (await conn.execute(text("SELECT wear_cost_per_hour FROM printers"))).scalar_one()
        stamped = (await conn.execute(text("SELECT wear_cost, wear_rate FROM print_archives"))).one()
    await engine.dispose()

    # Nullable, no default: an existing printer has no rate and an existing print no figure.
    assert printers["wear_cost_per_hour"] == ("FLOAT", 0)
    assert archives["wear_cost"] == ("FLOAT", 0)
    assert archives["wear_rate"] == ("FLOAT", 0)
    assert rate is None
    assert tuple(stamped) == (None, None)


def test_the_migration_has_no_seed():
    # No backfill: a rate applies to prints that end after it was set.
    assert not hasattr(m246, "seed")
