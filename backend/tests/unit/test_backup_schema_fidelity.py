"""The portable SQLite backup must carry the FULL schema, not just names+types.

On a PostgreSQL install ``dump_to_sqlite`` writes a portable SQLite copy so
backups move between engines. Restoring one onto a SQLite install page-copies
that schema onto the live database, and the post-restore ``init_db()`` cannot
repair it (``create_all`` is ``CREATE TABLE IF NOT EXISTS``). A schema missing
DEFAULT / NOT NULL therefore survives forever: every ``server_default`` column
silently took NULL on insert and the next read 500'd on Pydantic validation
(upstream #2526).

These tests inspect the DDL the export actually emits, via
``metadata.create_all`` + ``PRAGMA table_info`` / ``sqlite_master``.
"""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine

from backend.app.core.database import Base

# PRAGMA table_info column indices.
_TYPE, _NOTNULL, _DFLT = 2, 3, 4


@pytest.fixture(scope="module")
def backup_schema(tmp_path_factory):
    """The schema a portable backup gets, keyed table -> column -> PRAGMA row.

    Every model module has to be imported for the tables to be registered on
    ``Base.metadata`` — ``core/database.py`` does that inside ``init_db()``,
    not at import time, so importing the module alone yields an empty metadata.
    Walk the package instead, which also keeps this from silently under-testing
    when a new model file is added.
    """
    import importlib
    import pkgutil

    import backend.app.models as models_pkg

    for mod in pkgutil.iter_modules(models_pkg.__path__):
        importlib.import_module(f"{models_pkg.__name__}.{mod.name}")

    db_path = tmp_path_factory.mktemp("backup") / "schema.db"
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()

    conn = sqlite3.connect(str(db_path))
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        schema = {t: {row[1]: row for row in conn.execute(f"PRAGMA table_info({t})")} for t in tables}
        schema["__sql__"] = dict(  # type: ignore[assignment]
            conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table'")
        )
        yield schema
    finally:
        conn.close()


class TestBackupSchemaFidelity:
    def test_binary_column_is_blob(self, backup_schema):
        """``oidc_providers.icon_data`` is our only LargeBinary column. The old
        hand-rolled loop had no BLOB branch at all and mapped it to TEXT."""
        assert backup_schema["oidc_providers"]["icon_data"][_TYPE] == "BLOB"

    def test_server_default_column_keeps_its_default(self, backup_schema):
        """The #2526 failure mode: no DEFAULT -> SQLAlchemy omits the column on
        INSERT -> NULL -> the next read fails validation."""
        default = backup_schema["settings"]["created_at"][_DFLT]
        assert default is not None and "CURRENT_TIMESTAMP" in default.upper()

    def test_not_null_non_pk_column_stays_not_null(self, backup_schema):
        assert backup_schema["printers"]["serial_number"][_NOTNULL] == 1

    def test_unique_constraint_survives(self, backup_schema):
        assert "UNIQUE (serial_number)" in backup_schema["__sql__"]["printers"]

    def test_foreign_keys_survive(self, backup_schema):
        assert "FOREIGN KEY" in backup_schema["__sql__"]["print_queue"]


class TestPortableExportEndToEnd:
    @pytest.mark.asyncio
    async def test_export_emits_full_ddl_and_fills_null_defaults(self, tmp_path, monkeypatch):
        """Drive the real export. A source row holding NULL in a column the model
        declares NOT-NULL-with-default must land non-NULL rather than aborting
        the whole backup — the hazard `create_all` introduces by enforcing what
        the hand-rolled loop silently dropped."""
        from sqlalchemy.ext.asyncio import create_async_engine

        from backend.app.core import db_portable
        from backend.app.core.database import Base
        from backend.app.models.settings import Settings  # noqa: F401  (registers the table)

        # Stand-in for the PostgreSQL source: a permissive SQLite DB holding a
        # row with NULL created_at, which is what a nullable-added column looks
        # like on a migrated production database.
        src_path = tmp_path / "source.db"
        raw = sqlite3.connect(str(src_path))
        raw.execute(
            "CREATE TABLE settings (id INTEGER PRIMARY KEY, key TEXT, value TEXT, created_at TEXT, updated_at TEXT)"
        )
        raw.execute("INSERT INTO settings (key, value, created_at, updated_at) VALUES ('k', 'v', NULL, NULL)")
        raw.commit()
        raw.close()

        monkeypatch.setattr(db_portable, "is_sqlite", lambda: False, raising=False)
        monkeypatch.setattr("backend.app.core.db_dialect.is_sqlite", lambda: False)

        engine = create_async_engine(f"sqlite+aiosqlite:///{src_path}")
        out_path = tmp_path / "portable.db"
        settings_only = Base.metadata.tables["settings"].metadata.__class__()
        Base.metadata.tables["settings"].to_metadata(settings_only)
        try:
            await db_portable.dump_to_sqlite(engine, settings_only, out_path)
        finally:
            await engine.dispose()

        out = sqlite3.connect(str(out_path))
        try:
            created_at_row = next(r for r in out.execute("PRAGMA table_info(settings)") if r[1] == "created_at")
            assert created_at_row[_NOTNULL] == 1, "NOT NULL must survive into the portable file"
            assert "CURRENT_TIMESTAMP" in (created_at_row[_DFLT] or "").upper()

            stored = out.execute("SELECT key, created_at FROM settings").fetchall()
            assert stored and stored[0][0] == "k"
            assert stored[0][1] is not None, "a NULL in a NOT-NULL-with-default column must be filled, not raised"
        finally:
            out.close()

    @pytest.mark.asyncio
    async def test_sqlite_source_still_file_copies(self, tmp_path, monkeypatch):
        """The SQLite branch is untouched — it copies the live file, which is
        already full-fidelity."""
        from sqlalchemy.ext.asyncio import create_async_engine

        from backend.app.core import db_portable

        src_path = tmp_path / "live.db"
        raw = sqlite3.connect(str(src_path))
        raw.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY)")
        raw.commit()
        raw.close()

        monkeypatch.setattr("backend.app.core.db_dialect.is_sqlite", lambda: True)
        monkeypatch.setattr(
            "backend.app.core.config.settings.database_url", f"sqlite+aiosqlite:///{src_path}", raising=False
        )

        engine = create_async_engine(f"sqlite+aiosqlite:///{src_path}")
        out_path = tmp_path / "copy.db"
        try:
            await db_portable.dump_to_sqlite(engine, Base.metadata, out_path)
        finally:
            await engine.dispose()

        out = sqlite3.connect(str(out_path))
        try:
            names = [r[0] for r in out.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            assert "marker" in names
        finally:
            out.close()


class TestNullCoalescingIsTypeAware:
    """Only datetime server-defaults are substitutable. A boolean/integer
    server_default is an SQL expression we must not guess at — filling a
    timestamp into an integer column would be worse than the NULL."""

    def test_datetime_columns_are_substitutable(self):
        from sqlalchemy import Column, DateTime, func

        from backend.app.core.db_portable import _is_datetime_column

        assert _is_datetime_column(Column("created_at", DateTime, server_default=func.now())) is True

    def test_non_datetime_columns_are_not(self):
        from sqlalchemy import Boolean, Column, Integer, String

        from backend.app.core.db_portable import _is_datetime_column

        assert _is_datetime_column(Column("flag", Boolean, server_default="0")) is False
        assert _is_datetime_column(Column("count", Integer, server_default="0")) is False
        assert _is_datetime_column(Column("name", String(20), server_default="x")) is False
