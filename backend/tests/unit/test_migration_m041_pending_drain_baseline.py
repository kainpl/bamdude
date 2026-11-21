"""TA29 (G03 B): the historical upgrade through m041 with a pending upload, pinned as the baseline found it.

m041 drains ``pending_uploads`` into the library by constructing ``LibraryFile`` through the CURRENT model,
on a database that is still at m040. The INSERT names every column the model fills by default, and columns
added after m040 with a Python default — ``skip_objects_supported`` (m114) among them — do not exist yet, so
the drain fails and the upgrade stops there. That was already so before m253 (an existing defect outside
G03, recorded in its own TaskNote); m253's ``recovery_identity`` adds one more such column and must not change
the outcome: the drain fails with or without it.
"""

import pytest
from sqlalchemy import Column, MetaData, Table, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateTable

from backend.app.core.config import settings
from backend.app.migrations import m041_drain_pending_and_purge_archived as m041
from backend.app.models.library import LibraryFile

# Columns of the current model that a database at m040 does not have yet, with the migration that added each.
AFTER_M040 = {
    "skip_objects_supported": 114,
    "fs_modified_at": 129,
    "swap_source_declaration": 203,
    "swap_declaration_identity": 203,
    "external_url": 232,
    "recovery_identity": 250,
}


def _library_files_without(names: set[str]) -> str:
    metadata = MetaData()
    columns = [
        Column(c.name, c.type, primary_key=c.primary_key, nullable=c.nullable, server_default=c.server_default)
        for c in LibraryFile.__table__.columns
        if c.name not in names
    ]
    return str(CreateTable(Table("library_files", metadata, *columns)).compile(dialect=_sqlite_dialect()))


def _sqlite_dialect():
    from sqlalchemy.dialects import sqlite

    return sqlite.dialect()


async def _drain(tmp_path, monkeypatch, missing: set[str]):
    monkeypatch.setattr(settings, "base_dir", tmp_path)
    monkeypatch.setattr(settings, "library_dir", tmp_path / "library")
    (tmp_path / "library" / "files").mkdir(parents=True)
    (tmp_path / "library" / "thumbnails").mkdir(parents=True)
    upload = tmp_path / "pending" / "cube.stl"
    upload.parent.mkdir()
    upload.write_bytes(b"solid cube\nendsolid\n")

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm040.db'}")
    async with engine.begin() as conn:
        await conn.execute(text(_library_files_without(missing)))
        await conn.execute(
            text(
                "CREATE TABLE pending_uploads (id INTEGER PRIMARY KEY, filename VARCHAR(255), file_path VARCHAR(500), "
                "file_size INTEGER, status VARCHAR(20), archived_at DATETIME)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO pending_uploads (id, filename, file_path, file_size, status) "
                "VALUES (1, 'cube.stl', :path, 20, 'pending')"
            ),
            {"path": str(upload)},
        )
        await m041.upgrade(conn)
    try:
        with pytest.raises(OperationalError) as refused:
            await m041.seed(async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False))
        return str(refused.value.orig)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_database_at_m040_with_a_pending_upload_stops_at_m041(tmp_path, monkeypatch):
    message = await _drain(tmp_path, monkeypatch, set(AFTER_M040))
    assert "has no column named" in message


@pytest.mark.asyncio
async def test_it_stopped_there_before_m253_too(tmp_path, monkeypatch):
    """The same database with ``recovery_identity`` already present: the drain still fails, on a column of
    an earlier migration — the outcome is the baseline's, not m253's."""
    message = await _drain(tmp_path, monkeypatch, set(AFTER_M040) - {"recovery_identity"})
    assert "has no column named" in message and "recovery_identity" not in message
