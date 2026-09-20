"""A failed one-time SQLite → PostgreSQL import must stop startup, not fall
through to a fresh install (core/db_portable.py).

Seen 2026-09-07 with a real database: the import failed on one row, startup
carried on, every migration ran against the now-empty PostgreSQL, the defaults
were seeded and BamDude came up asking for setup — with the SQLite data intact
but invisible.
"""

from contextlib import asynccontextmanager

import pytest

from backend.app.core import db_portable
from backend.app.core.config import settings


class _Scalar:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _Engine:
    """engine.begin() whose only query answers SELECT COUNT(*) FROM printers."""

    def __init__(self, printers: int):
        self._printers = printers

    @asynccontextmanager
    async def begin(self):
        engine = self

        class Conn:
            async def execute(self, _clause):
                return _Scalar(engine._printers)

        yield Conn()


@pytest.fixture
def sqlite_file(tmp_path, monkeypatch):
    path = tmp_path / "bamdude.db"
    path.write_bytes(b"not really sqlite, never opened by these tests")
    monkeypatch.setattr(settings, "data_dir", tmp_path)

    async def candidate(_data_dir):
        return path

    monkeypatch.setattr(db_portable, "_local_sqlite_candidate", candidate)
    return path


async def test_not_postgres_does_nothing(monkeypatch, sqlite_file):
    monkeypatch.setattr("backend.app.core.db_dialect.is_postgres", lambda: False)
    assert await db_portable.auto_migrate_sqlite_to_pg(_Engine(0), None) is False
    assert sqlite_file.exists()


async def test_a_populated_postgres_is_never_imported_into(monkeypatch, sqlite_file):
    monkeypatch.setattr("backend.app.core.db_dialect.is_postgres", lambda: True)
    touched = []

    async def importer(*_a, **_k):
        touched.append(True)

    monkeypatch.setattr(db_portable, "import_sqlite_to_postgres", importer)
    assert await db_portable.auto_migrate_sqlite_to_pg(_Engine(printers=3), None) is False
    assert touched == [] and sqlite_file.exists()


async def test_import_failure_raises_and_leaves_the_sqlite_file_in_place(monkeypatch, sqlite_file):
    monkeypatch.setattr("backend.app.core.db_dialect.is_postgres", lambda: True)

    async def importer(*_a, **_k):
        raise RuntimeError("value too long for type character varying(64)")

    monkeypatch.setattr(db_portable, "import_sqlite_to_postgres", importer)
    with pytest.raises(db_portable.SqliteImportError) as exc:
        await db_portable.auto_migrate_sqlite_to_pg(_Engine(printers=0), None)
    message = str(exc.value)
    assert "bamdude.db" in message and "untouched" in message and "character varying(64)" in message
    assert sqlite_file.exists()
    assert not sqlite_file.with_suffix(".db.migrated").exists()


async def test_success_renames_the_sqlite_file(monkeypatch, sqlite_file):
    monkeypatch.setattr("backend.app.core.db_dialect.is_postgres", lambda: True)

    async def importer(*_a, **_k):
        return 78

    monkeypatch.setattr(db_portable, "import_sqlite_to_postgres", importer)
    assert await db_portable.auto_migrate_sqlite_to_pg(_Engine(printers=0), None) is True
    assert not sqlite_file.exists()
    assert sqlite_file.with_suffix(".db.migrated").exists()
