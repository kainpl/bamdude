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


class TestPortableRoundTripCarriesProductsAndOrders:
    """The projects redesign (m162) added eight NOT NULL columns whose
    ``server_default`` is an integer, not a datetime: ``products.is_active``,
    ``product_parts.qty_per_unit`` / ``auto`` / ``sort_order``,
    ``product_plates.plate_index``, ``project_lines.quantity`` /
    ``sort_order`` and ``project_procurement.quantity_acquired``.

    ``_export_pg_to_sqlite`` coalesces a NULL only from a column's Python-side
    ``default``, or from a datetime ``server_default``; an integer or boolean
    ``server_default`` alone it deliberately refuses to guess at, and the
    portable file's NOT NULL then aborts the backup. All eight declare BOTH a
    Python ``default=`` and a ``server_default=``, so they land in the
    substitutable half and no such NULL can reach the INSERT — but that is a
    property of eight model lines, which is exactly the sort of thing a later
    edit drops in passing. So: drive the real export over a product with parts
    and plates and an order with lines, and read the rows back.
    """

    @pytest.mark.asyncio
    async def test_a_product_with_parts_and_an_order_with_lines_survive_a_round_trip(self, tmp_path, monkeypatch):
        import importlib
        import pkgutil

        from sqlalchemy.ext.asyncio import create_async_engine

        import backend.app.models as models_pkg
        from backend.app.core import db_portable

        for mod in pkgutil.iter_modules(models_pkg.__path__):
            importlib.import_module(f"{models_pkg.__name__}.{mod.name}")

        # The PostgreSQL stand-in: the real schema, holding real rows.
        src_path = tmp_path / "source.db"
        src_sync = create_engine(f"sqlite:///{src_path}")
        try:
            Base.metadata.create_all(src_sync)
            with src_sync.begin() as conn:
                conn.exec_driver_sql("INSERT INTO customers (id, name) VALUES (1, 'ACME')")
                conn.exec_driver_sql("INSERT INTO products (id, name, is_active) VALUES (1, 'Lamp', 1)")
                conn.exec_driver_sql(
                    "INSERT INTO product_parts (id, product_id, kind, name, name_key, qty_per_unit, auto, "
                    "sort_order, aliases) VALUES (1, 1, 'printed', 'shade', 'shade', 1, 0, 0, '[\"shade\"]')"
                )
                conn.exec_driver_sql(
                    "INSERT INTO product_parts (id, product_id, kind, name, name_key, qty_per_unit, auto, "
                    "sort_order) VALUES (2, 1, 'purchased', 'M3', 'purchased:m3', 4, 0, 1)"
                )
                conn.exec_driver_sql(
                    "INSERT INTO projects (id, name, customer_id, status, priority) VALUES (1, 'Order 1', 1, "
                    "'active', 'normal')"
                )
                conn.exec_driver_sql(
                    "INSERT INTO project_lines (id, project_id, product_id, quantity, material, sort_order) "
                    "VALUES (1, 1, 1, 3, 'PETG', 0)"
                )
                conn.exec_driver_sql(
                    "INSERT INTO project_lines (id, project_id, product_id, quantity, sort_order) "
                    "VALUES (2, 1, 1, 7, 1)"
                )
                conn.exec_driver_sql(
                    "INSERT INTO project_procurement (project_id, product_part_id, quantity_acquired) VALUES (1, 2, 9)"
                )
        finally:
            src_sync.dispose()

        monkeypatch.setattr(db_portable, "is_sqlite", lambda: False, raising=False)
        monkeypatch.setattr("backend.app.core.db_dialect.is_sqlite", lambda: False)

        engine = create_async_engine(f"sqlite+aiosqlite:///{src_path}")
        out_path = tmp_path / "portable.db"
        try:
            await db_portable.dump_to_sqlite(engine, Base.metadata, out_path)
        finally:
            await engine.dispose()

        out = sqlite3.connect(str(out_path))
        try:
            # Row for row, values included — a coalesced integer would show up
            # here as the default rather than what was stored.
            assert out.execute("SELECT id, name, is_active FROM products").fetchall() == [(1, "Lamp", 1)]
            assert out.execute(
                "SELECT id, product_id, kind, name_key, qty_per_unit, auto, sort_order FROM product_parts ORDER BY id"
            ).fetchall() == [
                (1, 1, "printed", "shade", 1, 0, 0),
                (2, 1, "purchased", "purchased:m3", 4, 0, 1),
            ]
            assert out.execute("SELECT aliases FROM product_parts WHERE id = 1").fetchone()[0] == '["shade"]'
            assert out.execute("SELECT id, name, customer_id, status FROM projects").fetchall() == [
                (1, "Order 1", 1, "active")
            ]
            assert out.execute(
                "SELECT id, project_id, product_id, quantity, material, sort_order FROM project_lines ORDER BY id"
            ).fetchall() == [(1, 1, 1, 3, "PETG", 0), (2, 1, 1, 7, None, 1)]
            assert out.execute("SELECT * FROM project_procurement").fetchall() == [(1, 2, 9)]

            # And the DDL that makes those columns un-NULLable in the first
            # place has to arrive with them, or a restore loses the guarantee.
            for table, column in (
                ("products", "is_active"),
                ("product_parts", "qty_per_unit"),
                ("product_parts", "auto"),
                ("product_parts", "sort_order"),
                ("product_plates", "plate_index"),
                ("project_lines", "quantity"),
                ("project_lines", "sort_order"),
                ("project_procurement", "quantity_acquired"),
            ):
                row = next(r for r in out.execute(f"PRAGMA table_info({table})") if r[1] == column)
                assert row[_NOTNULL] == 1, f"{table}.{column} lost NOT NULL in the portable file"
                assert row[_DFLT] is not None, f"{table}.{column} lost its DEFAULT in the portable file"
        finally:
            out.close()
