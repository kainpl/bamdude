"""Portable database operations - cross-dialect backup, restore, and auto-migration.

Backups are always in portable SQLite format regardless of database backend.
Restore can import SQLite backups into both SQLite and PostgreSQL.
Auto-migration transfers data from local SQLite to PostgreSQL on first PG start.
"""

import asyncio
import logging
import os
import re
import sqlite3
import tempfile
from contextlib import asynccontextmanager, closing, contextmanager
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import MetaData, text

logger = logging.getLogger(__name__)


def _is_datetime_column(col) -> bool:
    """Whether the column stores a date/time, so a ``func.now()`` server-default
    can be substituted with a Python ``datetime``."""
    type_name = str(col.type).upper()
    return "TIMESTAMP" in type_name or "DATETIME" in type_name or type_name == "DATE"


@contextmanager
def _staged_output(output_path: Path):
    """Own the temporary file until a complete backup can replace the destination."""
    fd, name = tempfile.mkstemp(prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent)
    os.close(fd)
    staging = Path(name)
    try:
        yield staging
        os.replace(staging, output_path)
    finally:
        staging.unlink(missing_ok=True)


def _sqlite_connection(path: Path):
    # mode=ro refuses missing files instead of creating an empty "backup".
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def validate_sqlite_backup(path: Path, *, require_app_schema: bool = False) -> None:
    """Reject damaged/incompatible files before touching the destination or services.

    Old files without migration history remain supported. Their schema is owned
    by the file; the normal legacy bootstrap and pending migration chain apply.
    """
    from backend.app.migrations import _discover_migrations

    try:
        with closing(_sqlite_connection(path)) as src:
            if src.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("SQLite integrity check failed")
            tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not tables:
                raise ValueError("Backup database has no tables")
            if require_app_schema and not {"printers", "settings"} <= tables:
                raise ValueError("Backup is missing application tables")
            if "_migrations" in tables:
                versions = {r[0] for r in src.execute("SELECT version FROM _migrations")}
                known = {m["version"] for m in _discover_migrations()}
                if versions - known:
                    raise ValueError("Backup contains newer or unsupported migrations")
    except sqlite3.Error as exc:
        raise ValueError("Backup is not a readable SQLite database") from exc


def _snapshot_sqlite(source: Path, destination: Path) -> None:
    with closing(_sqlite_connection(source)) as src, closing(sqlite3.connect(destination)) as dst:
        src.backup(dst, pages=256)


async def _file_work(function, *args, **kwargs):
    """Join off-thread file work even on cancellation, before its directory disappears."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            if not cancelled:
                raise
            break
    if cancelled:
        if not task.cancelled() and task.exception() is not None:
            logger.error("File operation failed while backup was being cancelled: %s", task.exception())
        raise asyncio.CancelledError
    return task.result()


async def dump_to_sqlite(engine, metadata, output_path: Path) -> None:
    """Export a consistent database snapshot to the existing portable SQLite format."""
    from backend.app.core.db_dialect import is_sqlite

    if is_sqlite():
        # Read through SQLite, including committed WAL pages. Checkpoint + copy
        # can race writers and does not capture a consistent live database.
        with _staged_output(output_path) as staging:
            await _file_work(_snapshot_sqlite, Path(engine.url.database), staging)
    else:
        await _export_pg_to_sqlite(engine, metadata, output_path)


def _portable_metadata(metadata) -> MetaData:
    """Full canonical DDL, without altering the models used by bootstrap migrations."""
    from sqlalchemy import Column, DateTime, Integer, String, Table, func

    portable = MetaData()
    for table in metadata.tables.values():
        copy = table.to_metadata(portable)
        for col in list(copy.c):
            if col.info.get("migration_shim"):
                # These are standalone bootstrap columns. Refuse a future shim
                # that participates in a constraint/index rather than corrupt DDL.
                if col.primary_key or col.foreign_keys or any(col.name in i.columns for i in copy.indexes):
                    raise ValueError(f"Constrained migration shim: {table.name}.{col.name}")
                if any(col.name in c.columns for c in copy.constraints):
                    raise ValueError(f"Constrained migration shim: {table.name}.{col.name}")
                copy._columns.remove(col)
    Table(
        "_migrations",
        portable,
        Column("id", Integer, primary_key=True),
        Column("version", Integer, nullable=False, unique=True),
        Column("name", String(100), nullable=False),
        Column("applied_at", DateTime, server_default=func.current_timestamp()),
    )
    return portable


async def _check_export_schema(conn, portable, models) -> None:
    """Never silently drop source data that the portable schema cannot represent."""
    from sqlalchemy import inspect

    def inventory(sync_conn):
        inspector = inspect(sync_conn)
        return {name: {c["name"] for c in inspector.get_columns(name)} for name in inspector.get_table_names()}

    present = await conn.run_sync(inventory)
    expected = set(portable.tables)
    if set(present) != expected:
        raise ValueError(
            f"Portable schema mismatch: missing tables {sorted(expected - present.keys())}; "
            f"unhandled tables {sorted(present.keys() - expected)}"
        )
    if conn.dialect.name == "postgresql":
        from backend.app.migrations import _discover_migrations

        versions = set((await conn.execute(text("SELECT version FROM _migrations"))).scalars())
        if versions != {m["version"] for m in _discover_migrations()}:
            # Canonical DDL describes the current application, not an older
            # migration level. Never discard legacy shim values from a source
            # whose converters have not run yet.
            raise ValueError("Finish database migrations before creating a portable backup")
    for name, table in portable.tables.items():
        wanted = set(table.c.keys())
        ignored = (
            {c.name for c in models.tables[name].c if c.info.get("migration_shim")} if name in models.tables else set()
        )
        if name == "print_archives":
            ignored.add("search_vector")  # Rebuilt as SQLite FTS, never copied as user data.
        missing, extra = wanted - present[name], present[name] - wanted - ignored
        if missing or extra:
            raise ValueError(
                f"Portable schema mismatch in {name}: missing {sorted(missing)}; unhandled {sorted(extra)}"
            )


async def _export_pg_to_sqlite(engine, metadata, output_path: Path) -> None:
    """Export canonical DDL + data + migration history from one read-only snapshot."""
    import json

    from sqlalchemy import JSON, Text, cast, create_engine as create_sync_engine, select
    from sqlalchemy.ext.asyncio import create_async_engine

    portable = _portable_metadata(metadata)
    with _staged_output(output_path) as staging:
        async with engine.connect() as conn, conn.begin():
            if engine.dialect.name == "postgresql":
                # Issue before the first SELECT. Plain READ COMMITTED takes a
                # new snapshot for each table and can mix parent/child revisions.
                await conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            await _check_export_schema(conn, portable, metadata)
            # Staging engine: no case-folding listener (core/case_folding.py) — nothing
            # case-insensitive runs here; add configure_sqlite_connection if that ever changes.
            schema_engine = create_sync_engine(f"sqlite:///{staging}")
            try:
                portable.create_all(schema_engine)
            finally:
                schema_engine.dispose()
            with closing(sqlite3.connect(staging)) as dst:
                # Cyclic FKs require loading without enforcement. Constraints
                # remain in the portable DDL, and are checked after all rows land.
                dst.execute("PRAGMA foreign_keys = OFF")
                now = datetime.now()  # noqa: DTZ005
                for table in portable.tables.values():
                    columns = list(table.c)
                    defaults = {}
                    for col in columns:
                        if not col.nullable:
                            if col.default is not None:
                                default = col.default.arg
                                defaults[col.name] = default(None) if callable(default) else default
                            elif col.server_default is not None and _is_datetime_column(col):
                                defaults[col.name] = now
                    quoted = ", ".join(_quote(c.name) for c in columns)
                    placeholders = ", ".join("?" for _ in columns)
                    insert = f"INSERT INTO {_quote(table.name)} ({quoted}) VALUES ({placeholders})"  # noqa: S608

                    def serialize(row, columns=columns, defaults=defaults):
                        values = []
                        for col, value in zip(columns, row, strict=True):
                            if value is None and col.name in defaults:
                                value = defaults[col.name]
                                if isinstance(col.type, JSON):
                                    value = json.dumps(value)
                            if isinstance(value, datetime):
                                value = value.isoformat(" ")
                            elif isinstance(value, date):
                                value = value.isoformat()
                            values.append(value)
                        return tuple(values)

                    # Read JSON as text, preserving SQL NULL vs JSON null and
                    # scalar strings/booleans without decoding/re-encoding them.
                    statement = select(
                        *(cast(c, Text).label(c.name) if isinstance(c.type, JSON) else c for c in columns)
                    )
                    async with conn.stream(statement) as result:
                        async for rows in result.partitions(500):
                            dst.executemany(insert, [serialize(row) for row in rows])
                violations = dst.execute("PRAGMA foreign_key_check").fetchmany(5)
                if violations:
                    raise ValueError(f"Portable backup has foreign key violations: {violations}")
                dst.commit()
        if "print_archives" in portable.tables:
            from backend.app.migrations.m001_bamdude_baseline import _setup_sqlite_fts

            # Staging engine: no case-folding listener (core/case_folding.py) — nothing
            # case-insensitive runs here; add configure_sqlite_connection if that ever changes.
            fts_engine = create_async_engine(f"sqlite+aiosqlite:///{staging}")
            try:
                async with fts_engine.begin() as conn:
                    await _setup_sqlite_fts(conn)
                    # Explicit rebuild surfaces errors the legacy helper tolerates.
                    await conn.execute(text("INSERT INTO archive_fts(archive_fts) VALUES ('rebuild')"))
            finally:
                await fts_engine.dispose()
        await _file_work(validate_sqlite_backup, staging)
    logger.info("PostgreSQL exported to portable SQLite: %s", output_path)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _pg_predicate(sql: str, booleans: set[str]) -> str:
    """A SQLite boolean test, spelled for PostgreSQL.

    SQLite has no boolean type, so its migrations wrote ``is_active = 1`` and
    ``auto_link_existing_accounts = 0`` — and reflection hands those back
    verbatim, inside CHECK constraints and partial-index WHERE clauses.
    PostgreSQL has no ``boolean = integer`` operator and refuses the DDL
    (measured on three real files, 2026-09-09). Only the table's own boolean
    columns are rewritten, and only against a bare or quoted 0/1; everything
    else in the predicate is left exactly as the file had it.
    """
    if not booleans:
        return sql
    names = "|".join(re.escape(c) for c in sorted(booleans, key=len, reverse=True))
    pattern = re.compile(r"(?<![\w.])(" + names + r")\s*(=|!=|<>)\s*'?([01])'?(?![\w'])")
    return pattern.sub(lambda m: f"{m.group(1)} {m.group(2)} {'true' if m.group(3) == '1' else 'false'}", sql)


def _reflect_sqlite_schema(sqlite_path: Path, models_metadata) -> MetaData:
    """The SQLite file's OWN schema, translated for PostgreSQL.

    ⚠️ This replaced ``models_metadata.create_all``, and the difference is the
    whole point. The models describe the schema at the newest migration; a
    user's file is at whatever migration it last ran. Building the target from
    the models and then copying "the columns that fit" silently discarded every
    column and table the models had since retired — and those are precisely
    the SOURCES the later migrations convert from. Measured 2026-09-09 on a
    real server: a project's file links, parts and plan vanished (m158's whole
    input), a channel's printer binding vanished (m157's ``printer_id``), and
    then the entire chain re-ran from m001 because ``_migrations`` was skipped
    too — which is how a dialect fault in m157 could take a tester's install
    down at all.

    So: reflect the file, import it at its own level WITH ``_migrations``, and
    ``init_db`` carries on from the next migration exactly as a SQLite upgrade
    would. What is translated, and why each:

    * A column the current model still declares takes the MODEL's type —
      every one, not a chosen few. SQLite enforces neither a ``VARCHAR``
      width nor a ``BOOLEAN``, so an upgraded file still says
      ``key_hash VARCHAR(64)`` from the SHA-256 days under 87-character
      pbkdf2 hashes, and ``is_active INTEGER`` under a model ``Boolean`` —
      PostgreSQL would refuse the row for the first and the partial index
      ``WHERE is_active = TRUE`` for the second (measured on four real files,
      2026-09-09). The model is also how ``Column(JSON)`` comes out ``json``
      rather than the ``TEXT`` SQLite stored it as. A column the models no
      longer know keeps its reflected type, with ``DATETIME`` → ``TIMESTAMP``
      and ``BLOB`` → ``BYTEA`` (PostgreSQL has neither spelling): it is on
      its way to being converted and dropped by a migration, which is the
      only reader it has left.
    * A boolean's ``DEFAULT 0|1`` becomes ``false|true``, and the boolean
      tests inside CHECKs and partial-index predicates likewise — see
      ``_pg_predicate``.
    * ``INTEGER PRIMARY KEY`` reflects with ``autoincrement="auto"`` and
      renders ``SERIAL`` — the ``<table>_id_seq`` names the sequence reset in
      Phase 3 already expects.

    Foreign keys are reflected and emitted as-is; Phase 1 below drops them
    from the catalogue for the load and Phase 3 restores them, so load order
    does not matter. Reflection cannot carry an expression index (it warns
    and skips), which ``conform_imported_schema`` covers after the chain.
    """
    from sqlalchemy import TIMESTAMP, Boolean, CheckConstraint, MetaData, create_engine, text as sa_text
    from sqlalchemy.dialects.postgresql import BYTEA
    from sqlalchemy.schema import DefaultClause
    from sqlalchemy.types import DateTime, LargeBinary, _Binary

    reflected = MetaData()
    # Staging engine: no case-folding listener (core/case_folding.py) — nothing
    # case-insensitive runs here; add configure_sqlite_connection if that ever changes.
    sync_engine = create_engine(f"sqlite:///{sqlite_path}")
    try:
        reflected.reflect(bind=sync_engine)
    finally:
        sync_engine.dispose()

    for name in list(reflected.tables):
        if name.startswith("sqlite_") or name.startswith("archive_fts"):
            reflected.remove(reflected.tables[name])
            continue
        table = reflected.tables[name]
        model_table = models_metadata.tables.get(name)
        for column in table.columns:
            model_col = model_table.columns.get(column.name) if model_table is not None else None
            if model_col is not None:
                column.type = model_col.type.copy()
            elif isinstance(column.type, DateTime):
                column.type = TIMESTAMP()
            elif isinstance(column.type, (LargeBinary, _Binary)):
                column.type = BYTEA()

            # SQLite-first migrations wrote ``BOOLEAN DEFAULT 0|1``; PostgreSQL
            # refuses an integer default on a boolean column ("default
            # expression is of type integer") and aborts the whole CREATE. Same
            # rule ``helpers._to_postgres_column_def`` applies to add_column,
            # here per reflected column — including the quoted spellings a
            # ``create_all`` on SQLite leaves behind ('0', '1', 'true').
            # ``DefaultClause``, not a bare TextClause: that is the shape
            # reflection produces and the only one the DDL compiler (and
            # SQLAlchemy's own repr) can read — a bare clause has no ``.arg``
            # and refuses ``__bool__``.
            if isinstance(column.type, Boolean) and column.server_default is not None:
                raw = str(getattr(column.server_default, "arg", column.server_default)).strip().strip("'").lower()
                if raw in ("0", "false", "f"):
                    column.server_default = DefaultClause(sa_text("false"))
                elif raw in ("1", "true", "t"):
                    column.server_default = DefaultClause(sa_text("true"))

        # The predicates the file carries — CHECK constraints and the WHERE of
        # a partial index — in PostgreSQL's spelling of a boolean test (see
        # _pg_predicate). A partial index reflects with ``sqlite_where`` only,
        # which PostgreSQL DDL ignores: without the copy below a UNIQUE partial
        # index would come across as a UNIQUE index over the whole table.
        booleans = {c.name for c in table.columns if isinstance(c.type, Boolean)}
        for con in [c for c in table.constraints if isinstance(c, CheckConstraint)]:
            table.constraints.discard(con)
            CheckConstraint(sa_text(_pg_predicate(str(con.sqltext), booleans)), name=con.name, table=table)
        for index in table.indexes:
            where = index.kwargs.get("sqlite_where")
            if where is not None:
                index.dialect_options["postgresql"]["where"] = sa_text(_pg_predicate(str(where), booleans))

    # ⚠️ The foreign keys stay ON the reflected schema. Stripping them from the
    # Table objects was tried and does not work: ``create_all`` emits the FKs
    # of the cyclic group (auto_queue_items ↔ library_files ↔ library_folders ↔
    # print_archives ↔ print_queue) as separate ``ALTER TABLE ... ADD`` built
    # from the metadata's own FK registry, which that surgery never reaches —
    # the compiled ``CreateTable`` looked clean and the catalogue still had
    # every constraint (measured on a real file, 2026-09-09). Phase 1 drops
    # them from the CATALOGUE after creation and Phase 3 puts them back from
    # ``pg_get_constraintdef``, exactly as before; that is immune to how
    # SQLAlchemy chooses to emit DDL.
    return reflected


async def _restore_model_indexes(conn, models_metadata) -> None:
    """Create any model-declared index the reflected DDL could not carry.

    SQLAlchemy skips expression-based indexes on reflection (it says so, once,
    as a warning). On a file already at the newest migration nothing would ever
    recreate it — every ``CREATE INDEX IF NOT EXISTS`` in the chain has already
    run. ``checkfirst`` keeps this a no-op for everything that did come across.
    """
    from sqlalchemy import inspect as sqla_inspect
    from sqlalchemy.sql import visitors
    from sqlalchemy.sql.elements import ColumnClause

    def _do(sync_conn):
        inspector = sqla_inspect(sync_conn)
        existing_tables = set(inspector.get_table_names())
        for table in models_metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            # ⚠️ The columns that exist in PostgreSQL right now, not the
            # model's. This used to read ``table.columns`` (the model), which
            # made the check below trivially true and, when this still ran
            # BEFORE the chain, created
            # ``ix_product_part_stock_movements_project_line_id`` on a database
            # three migrations short of having that column (measured on a real
            # 31-Aug file, 2026-09-09). It runs after the chain now, so the two
            # sets normally agree; the guard stays because it is cheap and the
            # failure it prevents is a boot that stops.
            present = {c["name"] for c in inspector.get_columns(table.name)}
            for index in table.indexes:
                # Every column the index touches, plain or inside an expression
                # (``COALESCE(a, b)`` names two), must already exist here.
                # ``visitors.iterate`` walks the expression tree and yields the
                # column clauses inside it; a plain Column yields itself.
                needed = {
                    node.name
                    for expr in index.expressions
                    for node in visitors.iterate(expr)
                    if isinstance(node, ColumnClause) and getattr(node, "name", None)
                }
                if needed and needed <= present:
                    index.create(sync_conn, checkfirst=True)

    await conn.run_sync(_do)


@asynccontextmanager
async def _fk_transaction(engine, connection=None):
    if connection is None:
        async with engine.begin() as conn:
            yield conn
    else:
        async with connection.begin_nested():
            yield connection


async def _add_foreign_key(
    engine, tbl, label, ddl, parent, ccols, pcols, set_null, repaired, *, connection=None
) -> str | None:
    """Run one ``ALTER TABLE … ADD … FOREIGN KEY``, repairing the rows that forbid it.

    A failure here is NOT cosmetic: it means the rows contain references the
    constraint forbids. SQLite does not enforce foreign keys unless
    ``PRAGMA foreign_keys=ON`` is set per connection (this codebase never
    does), so a long-lived database accumulates these silently — a plan item
    for a deleted project, say, which no screen can reach anyway. The repair
    is what the constraint would have done had it been enforced: an
    ``ON DELETE SET NULL`` reference is nulled, any other orphan row is
    deleted (NULL references are legal and left alone). Say how many and
    from where, then retry once. Refusing to migrate over unreachable junk
    would be worse; dropping it without a word would be worse still.

    Returns the error text if the constraint still cannot be added.
    """
    for attempt in (1, 2):
        try:
            async with _fk_transaction(engine, connection) as conn:
                await conn.execute(text(ddl))
            return None
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                logger.error("Could not add FK %s on %s: %s", label, tbl, e)
                return f"{tbl}.{label}: {e}"
            on = " AND ".join(f"p.{pc} = c.{cc}" for cc, pc in zip(ccols, pcols, strict=True))
            notnull = " AND ".join(f"c.{cc} IS NOT NULL" for cc in ccols)
            orphans = f"WHERE {notnull} AND NOT EXISTS (SELECT 1 FROM {parent} p WHERE {on})"
            if set_null:
                assigns = ", ".join(f"{cc} = NULL" for cc in ccols)
                repair = f"UPDATE {tbl} c SET {assigns} {orphans}"  # noqa: S608
            else:
                repair = f"DELETE FROM {tbl} c {orphans}"  # noqa: S608
            async with _fk_transaction(engine, connection) as conn:
                res = await conn.execute(text(repair))
            if res.rowcount:
                verb = "Nulled" if set_null else "Purged"
                repaired.append(f"{tbl}: {res.rowcount} row(s) referencing missing {parent} ({verb.lower()})")
                logger.warning("%s %d orphaned row(s) in %s (dangling %s reference)", verb, res.rowcount, tbl, parent)
    return None


def _fk_rule(value) -> str:
    """``ondelete``/``onupdate`` normalised: PostgreSQL reports the default as NO ACTION, the models say None."""
    return (value or "NO ACTION").upper()


def _model_fk_rules(models_metadata) -> dict[tuple[str, tuple[str, ...]], tuple[str, str]]:
    """(table, constrained columns) -> (ON DELETE, ON UPDATE) as the models declare them."""
    rules = {}
    for table in models_metadata.tables.values():
        for fk in table.foreign_key_constraints:
            rules[(table.name, tuple(c.name for c in fk.columns))] = (_fk_rule(fk.ondelete), _fk_rule(fk.onupdate))
    return rules


async def _apply_ddl(engine, what: str, items: list[tuple[str, str]]) -> None:
    """Run each ``(label, statement)`` in its own transaction; one summary line, one error line per failure.

    Never raises: by the time this runs the import is done and the file
    renamed, so a raise would stop every later boot with nothing left to
    retry. Each thing that could not be added is named, and the database
    goes on without it — exactly as its SQLite did.
    """
    done, failed = [], []
    for label, sql in items:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(sql))
            done.append(label)
        except Exception as e:  # noqa: BLE001
            failed.append(f"{label}: {e}")
    if done:
        logger.info("Conformed to the models — %s (%d): %s", what, len(done), "; ".join(done))
    for f in failed:
        logger.error("Could not conform to the models — %s: %s", what, f)


async def _reconcile_columns(engine, models_metadata) -> None:
    """Add every model column the database lacks, then match NOT NULL to the model.

    A SQLite ``add_column`` cannot say NOT NULL without a default, and a
    table a migration created by hand rarely repeats every NOT NULL the
    model has — so an upgraded file is looser than a fresh one. Tightening
    fails on a column that holds NULLs today, and that is reported and
    left; loosening (the model went nullable, the file did not follow — the
    ``password_hash`` of m002) always succeeds. Primary keys are never touched.
    """
    from sqlalchemy import inspect as sqla_inspect

    ddl = engine.dialect.ddl_compiler(engine.dialect, None)

    def _plan(sync_conn):
        inspector = sqla_inspect(sync_conn)
        existing = set(inspector.get_table_names())
        adds, nulls = [], []
        for table in models_metadata.tables.values():
            if table.name not in existing:
                continue
            present = {c["name"]: c for c in inspector.get_columns(table.name)}
            for col in table.columns:
                if col.info.get("migration_shim"):
                    # Declared for the ORM's sake mid-chain only (notification.py);
                    # a migration drops it, and a fresh install ends without it.
                    continue
                if col.name not in present:
                    adds.append(
                        (
                            f"{table.name}.{col.name}",
                            f"ALTER TABLE {table.name} ADD COLUMN {ddl.get_column_specification(col)}",
                        )
                    )
                elif not col.primary_key and bool(present[col.name]["nullable"]) != bool(col.nullable):
                    verb = "DROP" if col.nullable else "SET"
                    nulls.append(
                        (
                            f"{table.name}.{col.name} {verb} NOT NULL",
                            f"ALTER TABLE {table.name} ALTER COLUMN {col.name} {verb} NOT NULL",
                        )
                    )
        return adds, nulls

    async with engine.connect() as conn:
        adds, nulls = await conn.run_sync(_plan)
    await _apply_ddl(engine, "columns the models declare and the file did not carry", adds)
    await _apply_ddl(engine, "NOT NULL as the models declare it", nulls)


async def _reconcile_unique_constraints(engine, models_metadata) -> None:
    """Add every model UNIQUE the database does not enforce, by column set (a unique index counts)."""
    from sqlalchemy import UniqueConstraint, inspect as sqla_inspect
    from sqlalchemy.schema import AddConstraint

    def _plan(sync_conn):
        inspector = sqla_inspect(sync_conn)
        existing = set(inspector.get_table_names())
        items = []
        for table in models_metadata.tables.values():
            if table.name not in existing:
                continue
            present = {c["name"] for c in inspector.get_columns(table.name)}
            have = {tuple(u["column_names"]) for u in inspector.get_unique_constraints(table.name)}
            have |= {tuple(i["column_names"]) for i in inspector.get_indexes(table.name) if i.get("unique")}
            for con in table.constraints:
                if not isinstance(con, UniqueConstraint):
                    continue
                cols = tuple(c.name for c in con.columns)
                if cols not in have and set(cols) <= present:
                    items.append(
                        (f"{table.name}({', '.join(cols)})", str(AddConstraint(con).compile(dialect=engine.dialect)))
                    )
        return items

    async with engine.connect() as conn:
        items = await conn.run_sync(_plan)
    await _apply_ddl(engine, "UNIQUE constraints", items)


async def _reconcile_foreign_keys(engine, models_metadata) -> None:
    """Every model foreign key, with the model's ON DELETE / ON UPDATE rule.

    Matched by (table, constrained columns). One the database lacks is added
    (m144 ``smart_sensors.printer_id``, m166 ``products.origin_file_id`` —
    bare ``add_column("… INTEGER")``, because SQLite cannot put a constraint
    on an existing table without rebuilding it). One that exists with a
    different target or rule — SQLite never enforced the rule, so migrations
    rarely bothered to write it — is dropped and re-added the model's way.
    Rows that forbid a key are repaired by that same rule (``_add_foreign_key``).
    The name is PostgreSQL's own (``<table>_<col>_fkey``, what a fresh
    ``create_all`` gets).
    """
    from sqlalchemy import inspect as sqla_inspect
    from sqlalchemy.schema import AddConstraint

    def _plan(sync_conn):
        inspector = sqla_inspect(sync_conn)
        existing = set(inspector.get_table_names())
        items = []
        for table in models_metadata.tables.values():
            if table.name not in existing:
                continue
            present = {c["name"] for c in inspector.get_columns(table.name)}
            have = {tuple(fk["constrained_columns"]): fk for fk in inspector.get_foreign_keys(table.name)}
            for fk in table.foreign_key_constraints:
                cols = tuple(c.name for c in fk.columns)
                parent = fk.referred_table.name
                pcols = [e.column.name for e in fk.elements]
                if not set(cols) <= present or parent not in existing:
                    continue
                current = have.get(cols)
                drop = None
                if current is not None:
                    same = (
                        current["referred_table"] == parent
                        and list(current["referred_columns"]) == pcols
                        and _fk_rule(current.get("options", {}).get("ondelete")) == _fk_rule(fk.ondelete)
                        and _fk_rule(current.get("options", {}).get("onupdate")) == _fk_rule(fk.onupdate)
                    )
                    if same:
                        continue
                    drop = current["name"]
                items.append((table.name, list(cols), parent, pcols, fk, drop))
        return items

    async with engine.connect() as conn:
        items = await conn.run_sync(_plan)

    done, failed, repaired = [], [], []
    for tbl, cols, parent, pcols, fk, drop in items:
        label = f"{tbl}({', '.join(cols)}) -> {parent}" + (f" [was {drop}]" if drop else "")
        if drop:
            try:
                async with engine.begin() as conn:
                    await conn.execute(text(f'ALTER TABLE {tbl} DROP CONSTRAINT "{drop}"'))
            except Exception as e:  # noqa: BLE001
                failed.append(f"{label}: {e}")
                continue
        ddl = str(AddConstraint(fk).compile(dialect=engine.dialect))
        err = await _add_foreign_key(
            engine, tbl, label, ddl, parent, cols, pcols, _fk_rule(fk.ondelete) == "SET NULL", repaired
        )
        (failed if err else done).append(err or label)
    if done:
        logger.info("Conformed to the models — foreign keys (%d): %s", len(done), "; ".join(done))
    if repaired:
        logger.warning("Rows the imported file was carrying against those keys: %s", "; ".join(repaired))
    for f in failed:
        logger.error("Could not conform to the models — foreign key %s", f)


async def _reconcile_check_constraints(engine, models_metadata) -> None:
    """Add every named CHECK the models declare and the database does not have.

    A SQLite upgrade cannot add a CHECK to an existing table without
    rebuilding it, so a migration that declared one for fresh installs only
    (``ck_oidc_icon_triplet_co_null``) leaves the upgraded file without it.
    Matched by name; an unnamed model CHECK is not matched and not added.
    Rows that violate one are reported, not repaired — there is no right
    repair to guess.
    """
    from sqlalchemy import CheckConstraint, inspect as sqla_inspect
    from sqlalchemy.schema import AddConstraint

    def _plan(sync_conn):
        inspector = sqla_inspect(sync_conn)
        existing = set(inspector.get_table_names())
        items = []
        for table in models_metadata.tables.values():
            if table.name not in existing:
                continue
            have = {c["name"] for c in inspector.get_check_constraints(table.name)}
            for con in table.constraints:
                if isinstance(con, CheckConstraint) and con.name and con.name not in have:
                    items.append((f"{table.name}.{con.name}", str(AddConstraint(con).compile(dialect=engine.dialect))))
        return items

    async with engine.connect() as conn:
        items = await conn.run_sync(_plan)
    await _apply_ddl(engine, "CHECK constraints", items)


# Set by a successful ``import_sqlite_to_postgres``, consumed by
# ``conform_imported_schema`` once the migration chain has run after it.
_conform_pending = False


async def conform_imported_schema(engine, models_metadata) -> None:
    """After an import and the chain that followed it: what a fresh install has, the moved one gets.

    A file that upgraded through the migrations is the models MINUS
    whatever a migration never did on SQLite, and PostgreSQL-only work a
    migration did do — but on SQLite, where it counted as applied:

    * a foreign key added as a bare ``add_column("… INTEGER")``, or written
      without the ``ON DELETE`` rule SQLite would not have enforced anyway;
    * a NOT NULL, UNIQUE or named CHECK that ``add_column`` / a hand-written
      ``CREATE TABLE`` left out;
    * a column that exists for PostgreSQL only — ``print_archives.search_vector``,
      with its GIN index, trigger function and trigger, which m001 creates
      on PostgreSQL and which archive search on PostgreSQL queries directly;
    * an expression index reflection could not carry.

    Measured against a fresh ``create_all`` + chain on the same PostgreSQL
    (2026-09-09): after this pass the two schemas differ in nothing that
    changes behaviour. Runs exactly once, after the chain that followed an
    ``import_sqlite_to_postgres`` in this process — never on an ordinary
    boot: migrations are frozen, and a working install is not repaired
    behind its back.
    """
    global _conform_pending
    if not _conform_pending:
        return
    _conform_pending = False

    # m001's PostgreSQL half: the tsvector column, its index, the trigger
    # function and the trigger, plus the backfill — every statement of it is
    # idempotent, which is why the migration's own function is called rather
    # than copied. On the file m001 is "applied", but it was applied on
    # SQLite, where it built an FTS5 table that stayed behind.
    from backend.app.migrations.m001_bamdude_baseline import _setup_postgres_fts

    async with engine.begin() as conn:
        await _setup_postgres_fts(conn)
    logger.info("Conformed to the models — PostgreSQL archive search (tsvector, index, trigger)")

    await _reconcile_columns(engine, models_metadata)
    await _reconcile_unique_constraints(engine, models_metadata)
    await _reconcile_foreign_keys(engine, models_metadata)
    async with engine.begin() as conn:
        await _restore_model_indexes(conn, models_metadata)
    await _reconcile_check_constraints(engine, models_metadata)


def _request_conform() -> None:
    global _conform_pending
    _conform_pending = True


async def import_sqlite_to_postgres(engine, metadata, sqlite_path: Path) -> int:
    """Import a SQLite file into PostgreSQL at the FILE's migration level.

    Used for the first-start move and for restoring a SQLite backup. The
    schema is reflected from the file (``_reflect_sqlite_schema``), the rows
    are copied verbatim, ``_migrations`` comes along, and the caller's
    ``init_db`` then applies only what the file had not yet seen. ``metadata``
    is the models' metadata, consulted for column TYPES only — never for which
    tables or columns exist; that is the file's business.

    Drops and recreates tables without FKs, imports data, then restores FKs
    in ONE transaction: a failed import leaves the previous PostgreSQL intact.

    Returns number of tables imported.
    """
    validate_sqlite_backup(sqlite_path)

    with closing(_sqlite_connection(sqlite_path)) as src:
        src.row_factory = sqlite3.Row
        # The schema to build is the file's own. ``_migrations`` is deliberately
        # IN: it is what lets the chain resume at the right place instead of
        # replaying from m001 against data that has already been through it.
        reflected = _reflect_sqlite_schema(sqlite_path, metadata)
        sorted_tables = [t.name for t in reflected.sorted_tables]

        # Phase 1: Drop and recreate the schema, then strip foreign keys IN THE
        # DATABASE before loading data.
        #
        # This used to remove the constraint objects from ``metadata`` and rely on
        # ``create_all`` emitting FK-free DDL. That silently fails for the cyclic
        # group (auto_queue_items ↔ library_files ↔ library_folders ↔ print_archives
        # ↔ print_queue): SQLAlchemy cannot inline a cycle, so it emits those FKs as
        # separate ALTER TABLE statements built from the column-level ``ForeignKey``
        # objects, which the metadata surgery never touched. Measured on a real
        # PostgreSQL: 99 constraints before, 20 still standing after the strip — and
        # the first insert into library_files then died on a folder_id violation,
        # aborting the whole migration.
        #
        # Dropping them from the catalogue instead is immune to how SQLAlchemy
        # chooses to emit DDL, and lets the rows land in any order.
        async with engine.begin() as conn:
            # On PostgreSQL, plain metadata.drop_all only enumerates ORM-defined tables
            # and emits non-CASCADE DROP TABLE. Orphan tables left over from removed
            # features (e.g. legacy spoolman_* whose FKs still reference printers) then
            # block the drop with DependentObjectsStillExistError, aborting the whole
            # restore. Drop every public-schema table with CASCADE first so the orphans
            # and their constraints come down alongside the ORM ones; restricted to
            # schemaname='public' so a shared Postgres instance with non-BamDude data
            # in other schemas isn't affected. SQLite is unaffected (no orphan-FK risk).
            from backend.app.core.db_dialect import is_postgres

            if is_postgres():
                # Cap how long DROP TABLE will wait for AccessExclusiveLock so any
                # residual concurrent writer (a per-printer MQTT client writing
                # reactively, a background loop that woke on its cadence) surfaces a
                # fast `lock_timeout` error instead of blocking the restore for the
                # default cadence or producing an AB/BA deadlock. SET LOCAL scopes
                # to this transaction only; outside this restore path the global
                # default (no timeout) applies. Pairs with the background-service
                # pause in routes/settings.py::restore_backup (#1... PG deadlock).
                await conn.execute(text("SET LOCAL lock_timeout = '10s'"))
                await conn.execute(
                    text(
                        "DO $$ DECLARE r RECORD; "
                        "BEGIN FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname = 'public') LOOP "
                        "EXECUTE 'DROP TABLE IF EXISTS public.' || quote_ident(r.tablename) || ' CASCADE'; "
                        "END LOOP; END $$;"
                    )
                )
            else:
                await conn.run_sync(metadata.drop_all)
                await conn.run_sync(reflected.drop_all)
            # The file's schema, at the file's level — see _reflect_sqlite_schema.
            await conn.run_sync(reflected.create_all)

            # Now strip the foreign keys from the catalogue itself, remembering each
            # definition verbatim so Phase 3 can put it back exactly as PostgreSQL
            # rendered it. ``pg_get_constraintdef`` gives us the full
            # ``FOREIGN KEY (...) REFERENCES ... ON DELETE ...`` clause. This is the
            # one place FKs are handled — see the note at the end of
            # ``_reflect_sqlite_schema`` for why it cannot be done on the metadata.
            saved_db_fks = []
            if is_postgres():
                rows = (
                    await conn.execute(
                        text(
                            "SELECT c.conrelid::regclass::text AS child, c.conname, "
                            "       pg_get_constraintdef(c.oid) AS cdef, "
                            "       c.confrelid::regclass::text AS parent, "
                            "       (SELECT array_agg(a.attname ORDER BY u.ord) "
                            "          FROM unnest(c.conkey) WITH ORDINALITY u(attnum, ord) "
                            "          JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = u.attnum) AS ccols, "
                            "       (SELECT array_agg(a.attname ORDER BY u.ord) "
                            "          FROM unnest(c.confkey) WITH ORDINALITY u(attnum, ord) "
                            "          JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = u.attnum) AS pcols "
                            "FROM pg_constraint c "
                            "WHERE c.contype = 'f' AND c.connamespace = 'public'::regnamespace"
                        )
                    )
                ).all()
                saved_db_fks = [(r[0], r[1], r[2], r[3], list(r[4]), list(r[5])) for r in rows]
                for tbl, conname, *_ in saved_db_fks:
                    await conn.execute(text(f'ALTER TABLE {tbl} DROP CONSTRAINT "{conname}"'))
                logger.info("Dropped %d foreign keys for the duration of the import", len(saved_db_fks))

            # Phase 2: Import data
            imported = 0
            for table_name in sorted_tables:
                cursor = src.execute(f"SELECT * FROM {_quote(table_name)}")  # noqa: S608
                rows = cursor.fetchmany(500)
                if not rows:
                    continue

                # Every source column exists in PostgreSQL now — the target IS the
                # file's schema. The intersection stays as a belt-and-braces check.
                src_columns = rows[0].keys()
                pg_table = reflected.tables.get(table_name)
                if pg_table is None:
                    continue
                pg_columns = {c.name for c in pg_table.columns}
                columns = [c for c in src_columns if c in pg_columns]
                if not columns:
                    continue

                col_list = ", ".join(_quote(c) for c in columns)
                param_list = ", ".join(f":{c}" for c in columns)
                insert_sql = text(
                    f"INSERT INTO {_quote(table_name)} ({col_list}) VALUES ({param_list})"  # noqa: S608
                )

                # Identify type conversions needed
                bool_columns = set()
                datetime_columns = set()
                not_null_defaults: dict[str, object] = {}

                for col in pg_table.columns:
                    if col.name not in columns:
                        continue
                    col_type = str(col.type).upper()
                    if col_type == "BOOLEAN":
                        bool_columns.add(col.name)
                    elif "TIMESTAMP" in col_type or col_type == "DATETIME":
                        datetime_columns.add(col.name)
                    if not col.nullable and col.default is not None:
                        default = col.default.arg
                        if callable(default):
                            default = default(None)
                        not_null_defaults[col.name] = default
                    elif not col.nullable and col.server_default is not None:
                        if col.name in datetime_columns:
                            not_null_defaults[col.name] = "__now__"

                now = datetime.now()  # noqa: DTZ005

                def _convert_row(
                    row, cols=columns, bools=bool_columns, dts=datetime_columns, nn=not_null_defaults, _n=now
                ):
                    result = {}
                    for c in cols:
                        val = row[c]
                        if val is None and c in nn:
                            val = _n if nn[c] == "__now__" else nn[c]
                        if val is not None:
                            if c in bools:
                                val = bool(val)
                            elif c in dts and isinstance(val, str):
                                try:
                                    val = datetime.fromisoformat(val)  # noqa: DTZ011
                                except ValueError:
                                    pass
                        result[c] = val
                    return result

                count = 0
                while rows:
                    batch = [_convert_row(row) for row in rows]
                    await conn.execute(insert_sql, batch)
                    count += len(batch)
                    rows = cursor.fetchmany(500)
                imported += 1
                logger.info("Imported %d rows into %s", count, table_name)

            # New tables have new sequences: reset only the ones that actually
            # exist, and fail the transaction if one cannot be restored.
            if is_postgres():
                for table_name in sorted_tables:
                    if "id" not in reflected.tables[table_name].c:
                        continue
                    sequence = (
                        await conn.execute(
                            text("SELECT pg_get_serial_sequence(:table, 'id')"), {"table": _quote(table_name)}
                        )
                    ).scalar()
                    if sequence:
                        max_id = (await conn.execute(text(f"SELECT MAX(id) FROM {_quote(table_name)}"))).scalar()  # noqa: S608
                        if max_id is not None:
                            await conn.execute(
                                text("SELECT setval(:sequence, :value)"), {"sequence": sequence, "value": max_id}
                            )

            # Phase 3: Put the foreign keys back, exactly as they were. One that the
            # rows forbid is repaired the way the key itself would have — see
            # _add_foreign_key — and one that still fails stops the import: a database
            # silently missing constraints is a worse outcome than a migration that
            # stops and says why.
            if is_postgres():
                failed, repaired = [], []
                model_rules = _model_fk_rules(metadata)
                for tbl, conname, cdef, parent, ccols, pcols in saved_db_fks:
                    ddl = f'ALTER TABLE {tbl} ADD CONSTRAINT "{conname}" {cdef}'
                    # The repair follows the MODEL's ON DELETE rule for this key where
                    # the model still has it — the file's own spelling rarely carries
                    # one (SQLite never enforced it), and conform_imported_schema is
                    # about to rewrite the key the model's way regardless.
                    file_rule = "SET NULL" if "ON DELETE SET NULL" in cdef.upper() else ""
                    rule = model_rules.get((tbl, tuple(ccols)), (file_rule, ""))[0]
                    err = await _add_foreign_key(
                        engine, tbl, conname, ddl, parent, ccols, pcols, rule == "SET NULL", repaired, connection=conn
                    )
                    if err:
                        failed.append(err)
                if failed:
                    raise RuntimeError(
                        f"{len(failed)} foreign key(s) could not be restored after import: {'; '.join(failed[:5])}"
                    )
                logger.info("Restored %d foreign keys", len(saved_db_fks))
                if repaired:
                    logger.warning(
                        "Import repaired orphaned rows the source database was carrying: %s", "; ".join(repaired)
                    )

        # What the models declare beyond the file — indexes reflection could not
        # carry, foreign keys no migration ever created — is added AFTER the
        # pending migrations, when the tables they belong to exist. See
        # conform_imported_schema.
        _request_conform()

        logger.info("Cross-database import complete: %d tables imported", imported)
        return imported


async def _local_sqlite_candidate(data_dir) -> Path | None:
    """The local SQLite file this install may copy into an empty PostgreSQL.

    ``bamdude.db`` when it is there. A *legacy-named* file counts only when it is
    BamDude's own 3.0.1-era database, which ``_is_bamdude_301`` recognises by its
    ``telegram_chats`` table.

    The probe is the whole point. This path consumes what it accepts — the file
    is renamed to ``.db.migrated`` and its WAL unlinked — so without the gate a
    genuine upstream Bambuddy database dropped into the data directory would be
    imported table-by-table on the strength of nothing more than sharing some
    table names, and consumed in the process. That is precisely the one-time
    import removed in 0.5.6, and it ran here *before* m000 was ever reached.

    Imported inside the function: ``migrations`` reaches into this module during
    startup, so importing it at module scope would close the cycle.
    """
    from backend.app.migrations import FOREIGN_BAMBUDDY_NOTICE, _find_legacy_database, _is_bamdude_301

    primary = Path(data_dir) / "bamdude.db"
    if primary.exists():
        return primary

    alt = _find_legacy_database(data_dir)
    if alt is None:
        return None
    if await _is_bamdude_301(alt):
        return alt

    logger.warning(FOREIGN_BAMBUDDY_NOTICE, alt)
    return None


class SqliteImportError(RuntimeError):
    """The one-time SQLite → PostgreSQL import failed; startup must not continue."""


async def auto_migrate_sqlite_to_pg(engine, metadata) -> bool:
    """Auto-migrate local SQLite database to PostgreSQL on first PG start.

    Called during startup when:
    - DATABASE_URL points to PostgreSQL
    - PostgreSQL is empty (no data)
    - Local bamdude.db exists

    Returns True if migration was performed.
    """
    from backend.app.core.config import settings
    from backend.app.core.db_dialect import is_postgres

    if not is_postgres():
        return False

    # Check if PG already has data
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text("SELECT COUNT(*) FROM printers"))
            if (result.scalar() or 0) > 0:
                return False  # PG already populated
    except Exception:
        return False  # Table doesn't exist yet or other error

    # Look for local SQLite database
    sqlite_path = await _local_sqlite_candidate(settings.data_dir)
    if sqlite_path is None:
        return False  # No local SQLite to migrate

    logger.info("Found local SQLite database: %s - migrating to PostgreSQL...", sqlite_path)

    try:
        imported = await import_sqlite_to_postgres(engine, metadata, sqlite_path)

        # Rename SQLite to .migrated to prevent re-import
        migrated_path = sqlite_path.with_suffix(".db.migrated")
        if migrated_path.exists():
            migrated_path.unlink()
        sqlite_path.rename(migrated_path)
        # Clean up WAL/SHM
        for suffix in ("-wal", "-shm"):
            wal = sqlite_path.parent / (sqlite_path.name + suffix)
            if wal.exists():
                wal.unlink()

        logger.info(
            "SQLite → PostgreSQL migration complete (%d tables). Original renamed to %s", imported, migrated_path.name
        )
        return True

    except Exception as e:
        # The import is one transaction: PostgreSQL is empty again and the
        # SQLite file untouched. Stopping here is the only safe answer — if
        # startup went on, the pending migrations would seed a *fresh* install
        # into the empty PostgreSQL (default groups, catalogues, "setup
        # required"), and the next start would try the import again on top of
        # those seeds. Seen 2026-09-07 with a real database.
        logger.error("SQLite → PostgreSQL migration failed: %s", e)
        raise SqliteImportError(
            f"Importing {sqlite_path.name} into PostgreSQL failed: {e}. PostgreSQL was left empty and "
            f"{sqlite_path.name} is untouched. Fix the cause and start BamDude again."
        ) from e
