"""The portable restore never asks SQLAlchemy to sort the schema (upstream 58ea7a36, adapted).

The models hold a foreign-key cycle — measured 2026-09-26 across nine tables
(auto_queue_items, library_files, library_folders, print_archives, print_queue,
printer_queues, printers, products, project_lines). ``MetaData.sorted_tables``
cannot order a cycle: it drops those edges, warns "Cannot correctly sort tables
... this warning may raise an error in a future release", and on a restore that
still relied on the order once put a child before its parent.

Neither restore step needs an order any more: the import strips every foreign
key before loading and puts them back after, and index creation is per table.
So neither consults ``sorted_tables`` — which removes the warning and the future
error without touching a single constraint in the models (``use_alter`` would
change the DDL of those keys on every PostgreSQL install).
"""

import sqlite3
import warnings

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core import db_portable
from backend.app.core.database import Base, import_all_models

SORT_WARNING = "Cannot correctly sort tables"


def _sort_warnings(caught) -> list[str]:
    return [str(w.message) for w in caught if SORT_WARNING in str(w.message)]


@pytest.fixture(scope="module", autouse=True)
def _models():
    import_all_models()


def test_the_models_still_hold_the_cycle():
    """The premise: if a later schema change breaks the cycle, this test file
    is guarding nothing and can go."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _ = Base.metadata.sorted_tables
    assert _sort_warnings(caught)


async def test_restoring_indexes_does_not_sort(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'target.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                await db_portable._restore_model_indexes(conn, Base.metadata)
    finally:
        await engine.dispose()
    assert _sort_warnings(caught) == []


async def test_importing_a_file_does_not_sort(tmp_path, monkeypatch):
    """The import into an empty target, through the real reflection of a file
    that carries the whole schema."""
    source = tmp_path / "backup.db"
    sync_source = create_async_engine(f"sqlite+aiosqlite:///{source}")
    async with sync_source.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await sync_source.dispose()
    with sqlite3.connect(source) as raw:
        raw.execute("INSERT INTO settings (key, value) VALUES ('probe', '1')")
        raw.execute("CREATE TABLE _migrations(version INTEGER)")
        raw.execute("INSERT INTO _migrations VALUES(41)")

    monkeypatch.setattr("backend.app.core.db_dialect.is_postgres", lambda: False)
    target = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'target.db'}")
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            imported = await db_portable.import_sqlite_to_postgres(target, Base.metadata, source)
    finally:
        await target.dispose()

    assert imported >= 1
    assert _sort_warnings(caught) == []
