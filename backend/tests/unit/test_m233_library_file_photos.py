"""m233 creates ``library_file_photos``, and the model declares the same table."""

from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m233_library_file_photos as m233


@pytest.mark.asyncio
async def test_m233_creates_the_table_with_an_id_that_is_never_reused(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm233.db'}")
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY)")
        await conn.exec_driver_sql("CREATE TABLE library_files (id INTEGER PRIMARY KEY, filename VARCHAR(255))")

        await m233.upgrade(conn)

        columns = {
            row[1]: (row[2], bool(row[3]))
            for row in (await conn.exec_driver_sql("PRAGMA table_info(library_file_photos)")).fetchall()
        }
        assert columns == {
            "id": ("INTEGER", False),
            "library_file_id": ("INTEGER", True),
            "stored_name": ("VARCHAR(64)", True),
            "original_name": ("VARCHAR(255)", False),
            "content_type": ("VARCHAR(32)", True),
            "file_size": ("INTEGER", True),
            "width": ("INTEGER", True),
            "height": ("INTEGER", True),
            "caption": ("VARCHAR(500)", False),
            "uploaded_by_id": ("INTEGER", False),
            "uploaded_by_name": ("VARCHAR(150)", False),
            "created_at": ("DATETIME", True),
        }
        indexes = {row[1] for row in (await conn.exec_driver_sql("PRAGMA index_list(library_file_photos)")).fetchall()}
        assert "ix_library_file_photos_library_file_id" in indexes

        # AUTOINCREMENT: the newest photo's id does not pass to the next one.
        insert = (
            "INSERT INTO library_file_photos (library_file_id, stored_name, content_type, file_size, width, height, "
            "created_at) VALUES (1, 'a.jpg', 'image/jpeg', 1, 1, 1, '2026-01-01 00:00:00')"
        )
        await conn.exec_driver_sql(insert)
        await conn.exec_driver_sql("DELETE FROM library_file_photos")
        await conn.exec_driver_sql(insert)
        assert (await conn.exec_driver_sql("SELECT id FROM library_file_photos")).scalar() == 2

        # idempotent (DEBUG=true re-runs the newest migration)
        await m233.upgrade(conn)
    await engine.dispose()


@pytest.mark.asyncio
async def test_postgresql_is_asked_for_types_it_has(monkeypatch):
    statements = []

    async def no_table(conn, name):
        return False

    async def record(sql):
        statements.append(" ".join(sql.split()))

    monkeypatch.setattr(m233, "table_exists", no_table)
    conn = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), exec_driver_sql=record)
    await m233.upgrade(conn)

    create = next(s for s in statements if s.startswith("CREATE TABLE"))
    assert "id SERIAL PRIMARY KEY" in create
    assert "created_at TIMESTAMP NOT NULL" in create
    assert "DATETIME" not in create and "AUTOINCREMENT" not in create
    assert "REFERENCES library_files(id) ON DELETE CASCADE" in create
    assert "REFERENCES users(id) ON DELETE SET NULL" in create


def test_the_model_declares_the_same_table():
    from backend.app.models.library_file_photo import LibraryFilePhoto

    table = LibraryFilePhoto.__table__
    assert table.name == "library_file_photos"
    assert table.dialect_options["sqlite"]["autoincrement"] is True
    declared = {c.name: (str(c.type), not c.nullable) for c in table.c}
    assert declared == {
        "id": ("INTEGER", True),
        "library_file_id": ("INTEGER", True),
        "stored_name": ("VARCHAR(64)", True),
        "original_name": ("VARCHAR(255)", False),
        "content_type": ("VARCHAR(32)", True),
        "file_size": ("INTEGER", True),
        "width": ("INTEGER", True),
        "height": ("INTEGER", True),
        "caption": ("VARCHAR(500)", False),
        "uploaded_by_id": ("INTEGER", False),
        "uploaded_by_name": ("VARCHAR(150)", False),
        "created_at": ("DATETIME", True),
    }
    foreign = {fk.parent.name: (fk.column.table.name, fk.ondelete) for fk in table.foreign_keys}
    assert foreign == {"library_file_id": ("library_files", "CASCADE"), "uploaded_by_id": ("users", "SET NULL")}
