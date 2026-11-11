"""m216 gives every API key and printer an immutable connector identity, and adds the connector's receipt tables.

OrcaBamDude spec r2 §8.1, addendum C3:
- **Existing SQLite database.** A nullable column, a separate random UUID per existing row (never one default
  shared by all), then a unique index -- no rebuild of ``printers`` / ``api_keys`` for NOT NULL.
- **Fresh install.** The model's default gives every new row its own UUID.
- **Re-runs.** A second run changes no identity already given, and seeds the server identity and storage epoch once.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.migrations import m216_connector_identity as m216

pytestmark = pytest.mark.asyncio


async def _indexes(conn, table: str) -> dict[str, dict]:
    return await conn.run_sync(lambda sync: {i["name"]: i for i in inspect(sync).get_indexes(table)})


async def _identities(conn, table: str) -> list[str | None]:
    return [row[0] for row in (await conn.execute(text(f"SELECT connector_identity FROM {table} ORDER BY id"))).all()]


async def test_an_older_database_gives_each_row_its_own_identity_once(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE printers (id INTEGER PRIMARY KEY, name VARCHAR(100))"))
            await conn.execute(text("CREATE TABLE api_keys (id INTEGER PRIMARY KEY, name VARCHAR(100))"))
            await conn.execute(
                text(
                    "CREATE TABLE settings (id INTEGER PRIMARY KEY, key VARCHAR(100) UNIQUE, value TEXT, "
                    "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
                )
            )
            for n in (1, 2, 3):
                await conn.execute(
                    text("INSERT INTO printers (id, name) VALUES (:n, :name)"), {"n": n, "name": f"P{n}"}
                )
            for n in (1, 2):
                await conn.execute(
                    text("INSERT INTO api_keys (id, name) VALUES (:n, :name)"), {"n": n, "name": f"K{n}"}
                )

        async with engine.begin() as conn:
            await m216.upgrade(conn)
        async with engine.begin() as conn:
            printers = await _identities(conn, "printers")
            keys = await _identities(conn, "api_keys")
            seeded = dict((await conn.execute(text("SELECT key, value FROM settings"))).all())

        for table_values in (printers, keys):
            assert all(value and str(uuid.UUID(value)) == value for value in table_values), table_values
            assert len(set(table_values)) == len(table_values)
        assert set(printers).isdisjoint(keys)
        assert set(seeded) == {m216.SERVER_ID_KEY, m216.STORAGE_EPOCH_KEY}
        assert all(str(uuid.UUID(value)) == value for value in seeded.values())

        async with engine.begin() as conn:  # idempotent: the DEBUG re-run of the latest migration must change nothing
            await m216.upgrade(conn)
        async with engine.begin() as conn:
            assert await _identities(conn, "printers") == printers
            assert await _identities(conn, "api_keys") == keys
            assert dict((await conn.execute(text("SELECT key, value FROM settings"))).all()) == seeded
            for table in ("printers", "api_keys"):
                index = (await _indexes(conn, table))[f"ix_{table}_connector_identity"]
                assert index["unique"] and index["column_names"] == ["connector_identity"]
            names = await conn.run_sync(lambda sync: set(inspect(sync).get_table_names()))
            assert {"connector_submissions", "connector_submission_entries"} <= names
            with pytest.raises(IntegrityError):  # the unique index holds against a copied identity
                await conn.execute(
                    text("INSERT INTO printers (id, name, connector_identity) VALUES (9, 'copy', :v)"),
                    {"v": printers[0]},
                )
    finally:
        await engine.dispose()


async def test_a_fresh_install_gives_every_new_row_a_new_identity(test_engine):
    from backend.app.models.api_key import APIKey
    from backend.app.models.printer import Printer

    sessions = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as db:
        printers = [
            Printer(name=f"P{n}", serial_number=f"S{n}", ip_address=f"192.0.2.{n}", access_code="x") for n in (1, 2)
        ]
        keys = [APIKey(name=f"K{n}", key_hash=f"h{n}", key_prefix=f"bd_{n}") for n in (1, 2)]
        db.add_all([*printers, *keys])
        await db.commit()
        values = [p.connector_identity for p in printers] + [k.connector_identity for k in keys]
    assert all(str(uuid.UUID(v)) == v for v in values)
    assert len(set(values)) == 4
    async with sessions() as db:
        assert (await db.scalar(select(Printer.connector_identity).where(Printer.id == printers[0].id))) == values[0]
