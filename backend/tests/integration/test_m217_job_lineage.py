"""m217 gives AutoQueue jobs an immutable identity and adds the lineage columns (host spec r2.2 §11 S2).

- **Existing SQLite database.** ``auto_queue_items.recovery_identity``: a nullable column, a separate random UUID per
  existing row, then a unique index. ``print_queue.source_auto_identity`` and ``print_archives.queue_item_identity`` /
  ``source_auto_identity``: nullable, indexed, left empty for history -- lineage is never back-filled from numeric ids.
- **Fresh install.** The model's default gives every new AutoQueue row its own UUID.
- **Re-runs.** A second run changes no identity already given.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.migrations import m217_job_lineage as m217

pytestmark = pytest.mark.asyncio


async def _indexes(conn, table: str) -> dict[str, dict]:
    return await conn.run_sync(lambda sync: {i["name"]: i for i in inspect(sync).get_indexes(table)})


async def _columns(conn, table: str) -> set[str]:
    return await conn.run_sync(lambda sync: {c["name"] for c in inspect(sync).get_columns(table)})


async def test_an_older_database_gets_identities_and_empty_lineage(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE auto_queue_items (id INTEGER PRIMARY KEY, status VARCHAR(20))"))
            await conn.execute(text("CREATE TABLE print_queue (id INTEGER PRIMARY KEY, recovery_identity VARCHAR(36))"))
            await conn.execute(
                text("CREATE TABLE print_archives (id INTEGER PRIMARY KEY, recovery_identity VARCHAR(36))")
            )
            for n in (1, 2, 3):
                await conn.execute(text("INSERT INTO auto_queue_items (id, status) VALUES (:n, 'pending')"), {"n": n})
            await conn.execute(text("INSERT INTO print_queue (id, recovery_identity) VALUES (1, 'q-1')"))
            await conn.execute(text("INSERT INTO print_archives (id, recovery_identity) VALUES (1, 'a-1')"))

        async with engine.begin() as conn:
            await m217.upgrade(conn)
        async with engine.begin() as conn:
            identities = [
                r[0] for r in (await conn.execute(text("SELECT recovery_identity FROM auto_queue_items"))).all()
            ]
            queue_lineage = (await conn.execute(text("SELECT source_auto_identity FROM print_queue"))).scalar_one()
            archive_lineage = (
                await conn.execute(text("SELECT queue_item_identity, source_auto_identity FROM print_archives"))
            ).one()

        assert all(value and str(uuid.UUID(value)) == value for value in identities), identities
        assert len(set(identities)) == 3
        assert queue_lineage is None  # history is never back-filled
        assert tuple(archive_lineage) == (None, None)

        async with engine.begin() as conn:  # idempotent: the DEBUG re-run of the latest migration must change nothing
            await m217.upgrade(conn)
        async with engine.begin() as conn:
            again = [r[0] for r in (await conn.execute(text("SELECT recovery_identity FROM auto_queue_items"))).all()]
            assert again == identities
            index = (await _indexes(conn, "auto_queue_items"))["ix_auto_queue_items_recovery_identity"]
            assert index["unique"] and index["column_names"] == ["recovery_identity"]
            assert (await _indexes(conn, "print_queue"))["ix_print_queue_source_auto_identity"]["column_names"] == [
                "source_auto_identity"
            ]
            archive_indexes = await _indexes(conn, "print_archives")
            assert archive_indexes["ix_print_archives_queue_item_identity"]["column_names"] == ["queue_item_identity"]
            assert archive_indexes["ix_print_archives_source_auto_identity"]["column_names"] == ["source_auto_identity"]
            with pytest.raises(IntegrityError):  # the unique index holds against a copied identity
                await conn.execute(
                    text("INSERT INTO auto_queue_items (id, status, recovery_identity) VALUES (9, 'x', :v)"),
                    {"v": identities[0]},
                )
    finally:
        await engine.dispose()


async def test_a_database_without_the_tables_is_left_alone(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}")
    try:
        async with engine.begin() as conn:
            await m217.upgrade(conn)
        async with engine.begin() as conn:
            names = await conn.run_sync(lambda sync: set(inspect(sync).get_table_names()))
        assert not names & {"auto_queue_items", "print_queue", "print_archives"}
    finally:
        await engine.dispose()


async def test_a_fresh_install_gives_every_auto_job_its_own_identity(test_engine):
    from backend.app.models.auto_queue import AutoQueueItem

    sessions = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as db:
        items = [AutoQueueItem(target_model="X1C", position=n) for n in (1, 2)]
        db.add_all(items)
        await db.commit()
        values = [item.recovery_identity for item in items]
    assert all(str(uuid.UUID(v)) == v for v in values)
    assert len(set(values)) == 2
    async with sessions() as db:
        stored = await db.scalar(select(AutoQueueItem.recovery_identity).where(AutoQueueItem.id == items[0].id))
    assert stored == values[0]
