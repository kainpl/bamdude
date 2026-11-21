"""m253: the deferral proof column of a queue row and a library row's own identity (G03 B, §A; TA29)."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m253_claim_proof_and_library_identity as m253
from backend.app.models.library import LibraryFile

ROWS = 1203  # more than two of the migration's batches


def test_identity():
    assert m253.version == 253
    assert m253.name == "claim_proof_and_library_identity"


async def _schema(conn):
    await conn.execute(text("CREATE TABLE print_queue (id INTEGER PRIMARY KEY, status VARCHAR(20))"))
    await conn.execute(text("CREATE TABLE library_files (id INTEGER PRIMARY KEY, file_path VARCHAR(500))"))
    await conn.execute(text("INSERT INTO print_queue (id, status) VALUES (1, 'pending')"))
    await conn.execute(
        text("INSERT INTO library_files (id, file_path) VALUES (:id, :path)"),
        [{"id": n, "path": f"library/{n}.3mf"} for n in range(1, ROWS + 1)],
    )


async def _identities(conn) -> dict[int, str | None]:
    rows = (await conn.execute(text("SELECT id, recovery_identity FROM library_files"))).all()
    return {row[0]: row[1] for row in rows}


@pytest.mark.asyncio
async def test_every_existing_library_row_gets_its_own_identity_and_a_second_run_changes_nothing(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm253.db'}")
    async with engine.begin() as conn:
        await _schema(conn)
        await m253.upgrade(conn)
    async with engine.begin() as conn:
        first = await _identities(conn)
        assert len(first) == ROWS
        assert all(value and len(value) == 36 for value in first.values())
        assert len(set(first.values())) == ROWS, "one identity per row"

        indexes = (await conn.execute(text("PRAGMA index_list(library_files)"))).all()
        unique = {row[1]: row[2] for row in indexes}
        assert unique.get("ix_library_files_recovery_identity") == 1

        columns = {row[1] for row in (await conn.execute(text("PRAGMA table_info(print_queue)"))).all()}
        assert "deferred_claim_token" in columns
        assert (await conn.execute(text("SELECT deferred_claim_token FROM print_queue"))).scalar() is None, (
            "NULL proves nothing: no backfill"
        )

        await m253.upgrade(conn)
        assert await _identities(conn) == first

        with pytest.raises(IntegrityError):
            await conn.execute(
                text("INSERT INTO library_files (id, file_path, recovery_identity) VALUES (:id, 'x', :identity)"),
                {"id": ROWS + 1, "identity": first[1]},
            )
    await engine.dispose()


@pytest.mark.asyncio
async def test_a_row_inserted_through_the_model_gets_an_identity_and_the_next_row_with_its_id_another(db_session):
    """The model default is the one writer: a fresh install and every ORM constructor get one; a row that
    takes a deleted row's id (SQLite hands out ``max(id)+1``) gets a different one."""

    def make(name: str) -> LibraryFile:
        return LibraryFile(filename=name, file_path=f"library/{name}", file_type="3mf", file_size=1)

    first = make("a.3mf")
    db_session.add(first)
    await db_session.commit()
    first_id, first_identity = first.id, first.recovery_identity
    assert first_identity and len(first_identity) == 36

    await db_session.delete(first)
    await db_session.commit()
    again = make("a.3mf")
    db_session.add(again)
    await db_session.commit()
    assert again.id == first_id, "the harness reuses the id, as production SQLite does"
    assert again.recovery_identity and again.recovery_identity != first_identity


@pytest.mark.asyncio
async def test_on_a_fresh_install_the_migration_finds_the_models_index_and_adds_no_second_one(test_engine):
    async with test_engine.begin() as conn:
        await m253.upgrade(conn)
        indexes = (await conn.execute(text("PRAGMA index_list(library_files)"))).all()
        covering = []
        for row in indexes:
            columns = [col[2] for col in (await conn.execute(text(f"PRAGMA index_info('{row[1]}')"))).all()]
            if columns == ["recovery_identity"]:
                covering.append((row[1], row[2]))
        assert covering == [("ix_library_files_recovery_identity", 1)]
