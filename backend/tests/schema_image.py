"""The application's schema for test databases: built once per process, copied into each.

``create_all`` of the whole model (150 tables, 205 indexes) cost ~0.6 s, and every test
that asked for a database paid it — most of the suite's wall time. Here it runs once per
process and per shape of ``Base.metadata``; the result is kept as a serialized SQLite
image, and each new database receives a copy in a few milliseconds. The copy is the same
database ``create_all`` would have built, byte for byte.

Used by ``conftest.test_engine`` and by every fixture that builds its own SQLite database
(``integration/queue_test_database.py``, the swap rigs); one source, so they cannot drift.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import aiosqlite
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from backend.app.core.database import Base, configure_sqlite_connection, import_all_models

_IMAGES: dict[tuple, bytes] = {}


def metadata_shape() -> tuple:
    """What ``create_all`` would build: each table with its columns, indexes, constraints.

    The key of the cached image, so a test that adds a table or a column to
    ``Base.metadata`` at run time gets a freshly built schema, never a stale copy.
    """
    return tuple(
        (
            name,
            tuple(column.name for column in table.columns),
            tuple(sorted(index.name or "" for index in table.indexes)),
            len(table.constraints),
        )
        for name, table in sorted(Base.metadata.tables.items())
    )


async def schema_image() -> bytes:
    """``create_all`` of the current ``Base.metadata``, serialized; built once per shape.

    ⚠️ Here and in ``load_schema`` aiosqlite's own thread is reached through its private
    ``_execute`` / ``_conn``: ``sqlite3.Connection.serialize`` / ``deserialize`` must run
    on the thread that owns the connection, and aiosqlite has no public door for it. A
    newer aiosqlite that renames them fails every database test at once, loudly.
    """
    import_all_models()
    shape = metadata_shape()
    image = _IMAGES.get(shape)
    if image is None:
        builder = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        event.listen(builder.sync_engine, "connect", configure_sqlite_connection)
        try:
            async with builder.connect() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await conn.commit()
                driver = (await conn.get_raw_connection()).driver_connection
                image = _IMAGES[shape] = await driver._execute(driver._conn.serialize)
        finally:
            await builder.dispose()
    return image


async def load_schema(engine: AsyncEngine) -> None:
    """Give a new SQLite test engine the application's schema, before anything else touches it.

    In memory: the image is loaded into the engine's one connection (``StaticPool``).
    Connection-level state — the pragmas and the Unicode ``lower``/``upper`` of the
    connect listener — belongs to the connection, not to the image, and survives the load.

    A file: a serialized image is byte for byte a database file, so a new (or empty) file
    is written from it before the first connection opens it, and every pooled connection
    sees the schema. ⚠️ Never ``deserialize`` into a file-backed connection: that one
    connection would turn into an in-memory copy while the others open the empty file.
    A file that already holds data gets ``create_all`` (``checkfirst``), as before.
    """
    image = await schema_image()
    database = engine.url.database
    if database in (None, "", ":memory:"):
        async with engine.connect() as conn:
            driver = (await conn.get_raw_connection()).driver_connection
            await driver._execute(driver._conn.deserialize, image)
        return
    path = Path(database)
    if not path.exists() or path.stat().st_size == 0:
        path.write_bytes(image)
        return
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def closed_sqlite_threads_have_exited(timeout: float = 1.0) -> None:
    """Wait until the thread of every closed aiosqlite connection has ended.

    A closed connection's thread still drains its queue up to the stop sentinel,
    answering each item through ``call_soon_threadsafe`` on the loop that queued it; the
    per-test event loop must not close while such a thread is alive ("RuntimeError: Event
    loop is closed"). Waiting for the threads themselves replaces a fixed
    ``asyncio.sleep(0.1)`` per test, which was both a guess and thousands of
    worker-seconds of every CI run.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while any(
        isinstance(thread, aiosqlite.Connection) and not thread._running and thread.is_alive()
        for thread in threading.enumerate()
    ):
        if loop.time() >= deadline:
            return
        await asyncio.sleep(0.001)
