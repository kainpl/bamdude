"""A migration's ``CREATE TABLE`` must parse on the dialect that reaches it.

``init_db()`` is ``Base.metadata.create_all()`` followed by the migration chain,
so for years every table a migration created also existed in the models: on
PostgreSQL ``create_all`` had already built it and the migration's SQLite-only
DDL was skipped by an ``IF NOT EXISTS`` / ``table_exists`` guard that never
fired. That made the DDL text look dialect-agnostic when it was not.

m158 broke the coincidence. It retires ``project_print_plan_items``,
``library_file_projects`` and ``library_folder_projects`` from the models —
the frozen migrations that create them (m016, m044) still run on a FRESH
install, ``create_all`` no longer pre-creates them, and m016's
``id INTEGER PRIMARY KEY AUTOINCREMENT, … created_at DATETIME`` reached
asyncpg, which cannot even parse it (``IF NOT EXISTS`` does not help — the
statement is parsed before the existence check). Every PostgreSQL install
created from scratch on this branch died there.

So the rule these tests pin is: **a ``CREATE TABLE`` whose table is absent from
``Base.metadata`` has no ``create_all`` behind it and must be PostgreSQL-valid
on the path PostgreSQL actually walks.** The scan below finds those statements
itself rather than naming them, so the next model removal is caught by the test
instead of by a user's first boot.

The migrations are frozen, so the SQLite DDL strings are pinned byte-for-byte:
the only permitted change is what a PostgreSQL install gets.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
import re
from pathlib import Path

import pytest

# ── The pinned originals ────────────────────────────────────────────────────
# Copied verbatim out of the released files. A released migration may not
# change what it does to an existing SQLite database, so a diff here is a bug
# in the change, not in the pin.

M016_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS project_print_plan_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    library_file_id INTEGER NOT NULL REFERENCES library_files(id) ON DELETE CASCADE,
    copies INTEGER NOT NULL DEFAULT 1,
    order_index INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_plan_library_file UNIQUE (library_file_id)
)
"""

M044_PLAN_SQLITE_DDL = """
CREATE TABLE project_print_plan_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    library_file_id INTEGER NOT NULL REFERENCES library_files(id) ON DELETE CASCADE,
    copies INTEGER NOT NULL DEFAULT 1,
    order_index INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_plan_project_file UNIQUE (project_id, library_file_id)
)
"""

#: Tokens SQLite accepts and PostgreSQL does not have at all. ``WITHOUT ROWID``
#: is the one that reads as harmless and is not: it is a storage decision only
#: SQLite has a concept of, and asyncpg fails on it exactly as it fails on
#: ``AUTOINCREMENT`` — at parse time, before ``IF NOT EXISTS`` can save it.
SQLITE_ONLY_TOKENS = ("AUTOINCREMENT", "DATETIME", "WITHOUT ROWID")

#: What PostgreSQL spells "this column numbers itself". ``BIGSERIAL`` contains
#: ``SERIAL`` and is listed anyway, because the point of the list is to be read.
POSTGRES_AUTOINCREMENT_TOKENS = ("SERIAL", "BIGSERIAL", "IDENTITY")


# ── Recording doubles ───────────────────────────────────────────────────────


class _Result:
    """The slice of a SQLAlchemy result the migrations actually call."""

    def __init__(self, rows: list[tuple]):
        self._rows = rows

    def fetchall(self) -> list[tuple]:
        return self._rows

    def all(self) -> list[tuple]:
        return self._rows

    def scalar(self):
        return self._rows[0][0] if self._rows else None

    def scalar_one(self):
        return self._rows[0][0] if self._rows else None

    def scalar_one_or_none(self):
        return self._rows[0][0] if self._rows else None


class _RecordingConn:
    """Captures every statement a migration would send to the server."""

    def __init__(self, answer=None):
        self.statements: list[str] = []
        self._answer = answer or (lambda sql: [])

    async def execute(self, clause, params=None):
        sql = str(clause)
        self.statements.append(sql)
        return _Result(self._answer(sql))

    async def exec_driver_sql(self, sql):
        self.statements.append(sql)
        return _Result(self._answer(sql))


# ── The scan ────────────────────────────────────────────────────────────────

_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?\s*\(",
    re.IGNORECASE,
)

_DIALECT_FUNCS = {"is_postgres": "postgres", "is_sqlite": "sqlite"}

_TERMINATORS = (ast.Return, ast.Raise, ast.Continue, ast.Break)


def _dialect_of_test(node: ast.expr) -> tuple[str, bool] | None:
    """``('postgres'|'sqlite', negated)`` for an ``if is_postgres()``-style test."""
    negated = False
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        negated, node = True, node.operand
    if isinstance(node, ast.Await):
        node = node.value
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _DIALECT_FUNCS:
        return _DIALECT_FUNCS[node.func.id], negated
    return None


def _child_statements(stmt: ast.stmt) -> list[ast.stmt]:
    nested: list[ast.stmt] = []
    for field in ("body", "orelse", "finalbody"):
        nested.extend(getattr(stmt, field, None) or [])
    for handler in getattr(stmt, "handlers", None) or []:
        nested.extend(handler.body)
    return nested


def _mark_lines(marks: dict[int, bool], first: int, last: int, reachable: bool) -> None:
    for line in range(first, last + 1):
        marks[line] = marks.get(line, False) or reachable


def _walk(stmts: list[ast.stmt], reachable: bool, marks: dict[int, bool]) -> None:
    """Mark each line with whether PostgreSQL can reach it.

    Handles the two shapes the migrations actually use: a nested
    ``if is_postgres(): … else: …`` block, and the early-return guard
    ``if is_postgres(): …; return`` that makes the whole rest of the function
    SQLite-only (m074, m094 both look like that).
    """
    for stmt in stmts:
        dialect = _dialect_of_test(stmt.test) if isinstance(stmt, ast.If) else None
        if dialect is not None:
            assert isinstance(stmt, ast.If)  # narrowed by the guard above
            kind, negated = dialect
            pg_in_body = (kind == "postgres") != negated
            marks[stmt.lineno] = marks.get(stmt.lineno, False) or reachable
            _walk(stmt.body, reachable and pg_in_body, marks)
            _walk(stmt.orelse, reachable and not pg_in_body, marks)
            if pg_in_body and not stmt.orelse and stmt.body and isinstance(stmt.body[-1], _TERMINATORS):
                reachable = False
            continue

        nested = _child_statements(stmt)
        if not nested:
            _mark_lines(marks, stmt.lineno, stmt.end_lineno or stmt.lineno, reachable)
            continue
        covered = {line for child in nested for line in range(child.lineno, (child.end_lineno or child.lineno) + 1)}
        for line in range(stmt.lineno, (stmt.end_lineno or stmt.lineno) + 1):
            if line not in covered:
                marks[line] = marks.get(line, False) or reachable
        _walk(nested, reachable, marks)


def _joined_str_text(node: ast.JoinedStr) -> str:
    """An f-string's full text, placeholders rendered as ``{}``.

    Needed because an f-string splits into several ``Constant`` pieces, and a
    ``CREATE TABLE`` header can then sit in a different node from the
    ``DATETIME`` further down — which would hide the token from the scan.
    """
    out = []
    for part in node.values:
        out.append(part.value if isinstance(part, ast.Constant) and isinstance(part.value, str) else "{}")
    return "".join(out)


def _ddl_strings(tree: ast.AST) -> list[tuple[str, int]]:
    """Every ``(text, lineno)`` in the module that contains a ``CREATE TABLE``."""
    inside_fstring: set[int] = set()
    joined: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            joined.append((_joined_str_text(node), node.lineno))
            inside_fstring.update(id(child) for child in ast.walk(node) if child is not node)
    found = [(text, line) for text, line in joined if "CREATE TABLE" in text.upper()]
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in inside_fstring
            and "CREATE TABLE" in node.value.upper()
        ):
            found.append((node.value, node.lineno))
    return found


def _module_level_ddl_constants(tree: ast.Module) -> dict[int, str]:
    """``lineno of the value node -> constant name`` for module-level DDL strings."""
    out: dict[int, str] = {}
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            continue
        target = stmt.targets[0]
        value = stmt.value
        if not isinstance(target, ast.Name):
            continue
        if isinstance(value, (ast.Constant, ast.JoinedStr)):
            out[value.lineno] = target.id
    return out


def _name_load_lines(tree: ast.AST) -> dict[str, list[int]]:
    loads: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            loads.setdefault(node.id, []).append(node.lineno)
    return loads


@pytest.fixture(scope="module")
def modelled_tables() -> set[str]:
    """Tables ``create_all`` builds — every model module has to be imported.

    ``core/database.py`` imports them inside ``init_db()``, not at import time,
    so importing the package alone leaves ``Base.metadata`` empty. Walking the
    package also keeps this from under-testing when a model file is added.
    """
    import backend.app.models as models_pkg

    importlib.import_module("backend.app.models")
    for module in pkgutil.iter_modules(models_pkg.__path__):
        importlib.import_module(f"backend.app.models.{module.name}")
    from backend.app.core.database import Base

    assert Base.metadata.tables, "no models registered — the walk above found nothing"
    return set(Base.metadata.tables)


@pytest.fixture(scope="module")
def migration_modules() -> list[tuple[str, ast.Module]]:
    """Every migration module, imported and parsed."""
    import backend.app.migrations as migrations_pkg

    out: list[tuple[str, ast.Module]] = []
    for module in sorted(pkgutil.iter_modules(migrations_pkg.__path__), key=lambda m: m.name):
        imported = importlib.import_module(f"backend.app.migrations.{module.name}")
        source = Path(imported.__file__).read_text(encoding="utf-8")
        out.append((module.name, ast.parse(source)))
    assert len(out) > 100, f"only {len(out)} migration modules discovered"
    return out


def _unmodelled_creates(name: str, tree: ast.Module, modelled: set[str]) -> list[tuple[str, str, bool]]:
    """``(table, ddl, postgres_reachable)`` for tables ``create_all`` will not build."""
    marks: dict[int, bool] = {}
    _walk(tree.body, True, marks)
    constants = _module_level_ddl_constants(tree)
    loads = _name_load_lines(tree)

    rows: list[tuple[str, str, bool]] = []
    for ddl, lineno in _ddl_strings(tree):
        tables = [t for t in _CREATE_TABLE_RE.findall(ddl) if t not in modelled]
        if not tables:
            continue
        constant = constants.get(lineno)
        if constant is None:
            reachable = marks.get(lineno, True)
        else:
            uses = [line for line in loads.get(constant, []) if line != lineno]
            # A constant nothing reads is dead DDL — report it rather than
            # quietly exempting it.
            reachable = any(marks.get(line, True) for line in uses) if uses else True
        rows.extend((table, ddl, reachable) for table in tables)
    return rows


# ── Tests ───────────────────────────────────────────────────────────────────


def test_no_migration_reaches_postgresql_with_sqlite_only_create_table(modelled_tables, migration_modules):
    """The rule this whole file exists for.

    A table that is still in the models is created by ``create_all`` before the
    chain runs, and its migration DDL is dead on both dialects. A table that is
    NOT is created by the migration itself — on PostgreSQL too — so its DDL has
    to parse there.
    """
    offenders: list[str] = []
    for name, tree in migration_modules:
        for table, ddl, pg_reachable in _unmodelled_creates(name, tree, modelled_tables):
            if not pg_reachable:
                continue
            hits = [token for token in SQLITE_ONLY_TOKENS if token in ddl.upper()]
            if hits:
                offenders.append(f"{name}: CREATE TABLE {table} reaches PostgreSQL carrying {hits}")
    assert not offenders, "SQLite-only DDL on the PostgreSQL path:\n  " + "\n  ".join(offenders)


def test_a_postgresql_twin_of_an_autoincrement_table_numbers_its_key_itself(modelled_tables, migration_modules):
    """⚠️ Parsing is not the whole rule — the PostgreSQL twin must still WORK.

    ``id INTEGER PRIMARY KEY`` parses perfectly on PostgreSQL and is a plain
    integer column: no sequence, no default, so the first INSERT that omits the
    id fails with a NOT NULL violation. On SQLite the same words are the rowid
    alias and number themselves, which is why ``AUTOINCREMENT`` is often dropped
    rather than translated when a dialect twin is written — the test above is
    happy (the token is gone) and the fresh install dies on its first row.

    So: wherever a migration creates an unmodelled table with ``AUTOINCREMENT``
    on one dialect, the DDL PostgreSQL actually reaches has to say ``SERIAL`` /
    ``BIGSERIAL`` / ``GENERATED … AS IDENTITY``.
    """
    checked: list[str] = []
    offenders: list[str] = []
    for name, tree in migration_modules:
        rows = _unmodelled_creates(name, tree, modelled_tables)
        numbering_itself = {table for table, ddl, _pg in rows if "AUTOINCREMENT" in ddl.upper()}
        for table, ddl, pg_reachable in rows:
            upper = ddl.upper()
            # The SQLite twin itself: it is not on the PostgreSQL path (the test
            # above is what proves that) and it is not what this asks about.
            if not pg_reachable or table not in numbering_itself or "AUTOINCREMENT" in upper:
                continue
            checked.append(f"{name}.{table}")
            if not any(token in upper for token in POSTGRES_AUTOINCREMENT_TOKENS):
                offenders.append(f"{name}: CREATE TABLE {table} reaches PostgreSQL with a key that numbers nothing")
    assert checked, "no AUTOINCREMENT table with a PostgreSQL twin was found — the scan is asserting nothing"
    assert not offenders, "a PostgreSQL primary key with no sequence behind it:\n  " + "\n  ".join(offenders)


def test_the_scan_still_sees_the_tables_m158_removed_from_the_models(modelled_tables, migration_modules):
    """Guards the guard: the scan is worthless if it finds nothing to look at.

    These three are exactly the tables m158 retires while m016/m044 keep
    creating them, so they must show up as unmodelled — if a future change puts
    a model back, the test above stops covering them and this says so.
    """
    seen: set[str] = set()
    for name, tree in migration_modules:
        seen.update(table for table, _ddl, _pg in _unmodelled_creates(name, tree, modelled_tables))
    assert {"project_print_plan_items", "library_file_projects", "library_folder_projects"} <= seen


def test_the_sqlite_ddl_of_m016_is_the_released_original():
    from backend.app.migrations import m016_project_print_plan as m016

    assert m016._CREATE_TABLE_SQLITE == M016_SQLITE_DDL


def test_the_sqlite_ddl_of_m044_is_the_released_original():
    from backend.app.migrations import m044_library_project_pivots as m044

    assert m044._PLAN_NEW_DDL == M044_PLAN_SQLITE_DDL


def test_m016_hands_postgresql_a_statement_it_can_parse():
    from backend.app.migrations import m016_project_print_plan as m016

    ddl = m016._CREATE_TABLE_POSTGRES.upper()
    assert not [token for token in SQLITE_ONLY_TOKENS if token in ddl]
    assert "SERIAL PRIMARY KEY" in ddl
    assert "TIMESTAMP" in ddl


def test_both_dialects_of_m016_describe_the_same_table():
    """Same columns in the same order, same unique constraint.

    m044 later reshapes that constraint by name on PostgreSQL
    (``DROP CONSTRAINT uq_plan_library_file``), so the name is load-bearing,
    not cosmetic.
    """
    from backend.app.migrations import m016_project_print_plan as m016

    def columns(ddl: str) -> list[str]:
        body = ddl[ddl.index("(") + 1 : ddl.rindex(")")]
        return [line.strip().split()[0] for line in body.splitlines() if line.strip() and "CONSTRAINT" not in line]

    assert columns(m016._CREATE_TABLE_POSTGRES) == columns(m016._CREATE_TABLE_SQLITE)
    for ddl in (m016._CREATE_TABLE_POSTGRES, m016._CREATE_TABLE_SQLITE):
        assert "CONSTRAINT uq_plan_library_file UNIQUE (library_file_id)" in ddl
        assert "IF NOT EXISTS" in ddl


@pytest.mark.parametrize("postgres", [True, False])
def test_m016_creates_the_plan_table_on_a_fresh_install_of_either_dialect(monkeypatch, postgres):
    """The fresh-install path, driven end to end against a recording connection.

    ``table_exists`` says no — which is what m158 made true — so the branch that
    used to be dead on PostgreSQL now runs, and it must run the right DDL.
    """
    import backend.app.migrations.helpers as helpers
    from backend.app.migrations import m016_project_print_plan as m016

    monkeypatch.setattr(m016, "is_postgres", lambda: postgres)
    monkeypatch.setattr(helpers, "is_postgres", lambda: postgres)

    import asyncio

    conn = _RecordingConn(answer=lambda sql: [])  # table_exists -> no rows -> absent
    asyncio.run(m016.upgrade(conn))

    created = [sql for sql in conn.statements if "CREATE TABLE" in sql.upper()]
    assert len(created) == 1, conn.statements
    assert created[0] == (m016._CREATE_TABLE_POSTGRES if postgres else m016._CREATE_TABLE_SQLITE)
    assert any("CREATE INDEX IF NOT EXISTS ix_project_print_plan_items_project_id" in sql for sql in conn.statements)


def test_m044_reshapes_the_plan_constraint_on_postgresql_without_any_create_table(monkeypatch):
    """m044's PostgreSQL path against the table m016 now leaves behind.

    On PostgreSQL the reshape is two ``ALTER TABLE``s — the SQLite recreate DDL
    (``_PLAN_NEW_DDL``) is never sent, which is why that constant is allowed to
    stay byte-identical SQLite text.
    """
    import asyncio

    import backend.app.migrations.helpers as helpers
    from backend.app.migrations import m044_library_project_pivots as m044

    monkeypatch.setattr(m044, "is_postgres", lambda: True)
    monkeypatch.setattr(helpers, "is_postgres", lambda: True)

    def answer(sql: str) -> list[tuple]:
        if "information_schema.columns" in sql:
            return []  # no legacy project_id column — fresh install
        if "COUNT(*)" in sql:
            return [(0,)]
        if "pg_constraint" in sql:
            return []  # uq_plan_project_file not there yet
        return []

    conn = _RecordingConn(answer=answer)
    asyncio.run(m044.upgrade(conn))

    joined = " ".join(conn.statements)
    assert "project_print_plan_items DROP CONSTRAINT IF EXISTS uq_plan_library_file" in joined
    assert "ADD CONSTRAINT uq_plan_project_file UNIQUE (project_id, library_file_id)" in joined
    assert not any("CREATE TABLE project_print_plan_items" in sql for sql in conn.statements)
    # The two pivots ARE created here now that create_all no longer does it.
    assert any("CREATE TABLE IF NOT EXISTS library_file_projects" in sql for sql in conn.statements)
    assert any("CREATE TABLE IF NOT EXISTS library_folder_projects" in sql for sql in conn.statements)
    for sql in conn.statements:
        assert not [token for token in SQLITE_ONLY_TOKENS if token in sql.upper()], sql


def test_recreate_table_never_sends_its_ddl_to_postgresql(monkeypatch):
    """What lets every ``recreate_table`` DDL stay SQLite-flavoured.

    m044's ``_PLAN_NEW_DDL``, m074's and m094's rebuild temps are all handed to
    this helper; on PostgreSQL it drops columns with ``ALTER TABLE`` and ignores
    the DDL entirely. Pinned here so the exemption the scan grants them rests on
    a tested fact rather than on reading the helper.
    """
    import asyncio

    import backend.app.migrations.helpers as helpers

    monkeypatch.setattr(helpers, "is_postgres", lambda: True)

    def answer(sql: str) -> list[tuple]:
        if "information_schema.columns" in sql:
            return [("id",), ("keep_me",), ("drop_me",)]
        return []

    conn = _RecordingConn(answer=answer)
    asyncio.run(helpers.recreate_table(conn, "some_table", M044_PLAN_SQLITE_DDL, "id, keep_me"))

    assert not any("CREATE TABLE" in sql.upper() for sql in conn.statements), conn.statements
    assert any("DROP COLUMN IF EXISTS drop_me" in sql for sql in conn.statements)
