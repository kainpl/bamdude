"""m234 removes the notes old purges left without their library file.

Until the purge paths were fixed, on SQLite (which runs no foreign-key action)
the trash retention sweep and the deletion of a user with their items removed
library files in bulk and left their ``library_file_notes`` rows behind. The
seed deletes every note whose file is gone, and no other.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.migrations.m234_orphan_library_file_notes import seed
from backend.app.models.library import LibraryFile
from backend.app.models.library_file_note import LibraryFileNote

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _file(db, name: str, *, trashed: bool = False) -> int:
    row = LibraryFile(
        filename=name,
        file_path=f"library/{name}",
        file_type="3mf",
        file_size=1,
        deleted_at=datetime.now(UTC).replace(tzinfo=None) if trashed else None,
    )
    db.add(row)
    await db.commit()
    return row.id


async def _note(db, file_id: int, body: str) -> None:
    db.add(LibraryFileNote(library_file_id=file_id, user_id=None, body=body))
    await db.commit()


async def _purge_as_the_old_sweep_did(db, file_id: int) -> None:
    """The file row alone, by statement: on SQLite its notes stay, on PostgreSQL the cascade takes them."""
    await db.execute(delete(LibraryFile).where(LibraryFile.id == file_id))
    await db.commit()


async def _bodies(db) -> list[str]:
    rows = await db.execute(select(LibraryFileNote.body).order_by(LibraryFileNote.id))
    return list(rows.scalars().all())


async def _run(test_engine) -> None:
    await seed(async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False))


async def test_notes_of_purged_files_go_and_every_other_note_stays(db_session, test_engine, caplog):
    live = await _file(db_session, "live.3mf")
    trashed = await _file(db_session, "trashed.3mf", trashed=True)
    purged_one = await _file(db_session, "purged-one.3mf")
    purged_two = await _file(db_session, "purged-two.3mf")
    await _note(db_session, live, "live: first")
    await _note(db_session, purged_one, "orphan: one")
    await _note(db_session, trashed, "trashed: still restorable")
    await _note(db_session, purged_two, "orphan: two a")
    await _note(db_session, purged_two, "orphan: two b")
    await _note(db_session, live, "live: second")
    await _purge_as_the_old_sweep_did(db_session, purged_one)
    await _purge_as_the_old_sweep_did(db_session, purged_two)
    sqlite = test_engine.dialect.name == "sqlite"
    if sqlite:
        assert len(await _bodies(db_session)) == 6, "the premise: SQLite left the notes behind"

    with caplog.at_level(logging.INFO, logger="backend.app.migrations.m234_orphan_library_file_notes"):
        await _run(test_engine)

    assert await _bodies(db_session) == ["live: first", "trashed: still restorable", "live: second"]
    if sqlite:
        assert any("3" in record.getMessage() for record in caplog.records), "the count removed is logged"


async def test_a_second_run_and_a_clean_database_remove_nothing(db_session, test_engine):
    await _run(test_engine)  # no notes at all
    live = await _file(db_session, "live.3mf")
    await _note(db_session, live, "kept")

    await _run(test_engine)
    await _run(test_engine)

    assert await _bodies(db_session) == ["kept"]
