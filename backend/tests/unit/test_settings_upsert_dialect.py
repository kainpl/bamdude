"""Every Settings upsert must survive the PostgreSQL compiler.

⚠️ Found in production 2026-09-08 on a fresh Windows install with the bundled
PostgreSQL: ``POST /auth/setup`` answered 500 with

    AttributeError: 'OnConflictDoUpdate' object has no attribute 'constraint_target'

``set_setup_completed`` built its statement with ``dialects.sqlite.insert``
whatever the backend was. The PostgreSQL compiler is then handed a SQLite
``OnConflictDoUpdate``, whose class has no ``constraint_target`` — so a fresh
PostgreSQL install could not create its admin **at all**.

It hid for a long time because every earlier PostgreSQL run migrated an existing
SQLite database: setup was already marked complete there, so the line never ran.
The whole test suite runs on SQLite, where the wrong dialect happens to be the
right one — which is exactly why these tests compile for PostgreSQL explicitly
instead of executing.
"""

import ast
import pathlib

import pytest
from sqlalchemy.dialects import postgresql, sqlite

from backend.app.core import db_dialect
from backend.app.models.settings import Settings

BACKEND_APP = pathlib.Path(__file__).resolve().parents[3] / "backend" / "app"


class _CapturingSession:
    """Takes the statement instead of running it — no database needed."""

    def __init__(self):
        self.statements = []

    async def execute(self, statement, *args, **kwargs):
        self.statements.append(statement)
        return None


@pytest.mark.parametrize(
    ("postgres", "dialect", "expected_sql"),
    [
        (True, postgresql.dialect(), "ON CONFLICT (KEY) DO UPDATE"),
        (False, sqlite.dialect(), "ON CONFLICT (KEY) DO UPDATE"),
    ],
)
@pytest.mark.asyncio
async def test_the_helper_compiles_on_the_backend_it_was_asked_for(monkeypatch, postgres, dialect, expected_sql):
    monkeypatch.setattr(db_dialect, "is_postgres", lambda: postgres)
    session = _CapturingSession()

    await db_dialect.upsert_setting(session, Settings, "setup_completed", "true")

    # The compile is the assertion: the crash was a compile-time AttributeError,
    # never an execution error.
    compiled = str(session.statements[0].compile(dialect=dialect))
    assert expected_sql in compiled.upper().replace('"', "")


@pytest.mark.asyncio
async def test_setup_completed_survives_the_postgresql_compiler(monkeypatch):
    """The exact call that returned 500 on a fresh PostgreSQL install."""
    from backend.app.api.routes import auth

    monkeypatch.setattr(db_dialect, "is_postgres", lambda: True)
    session = _CapturingSession()

    await auth.set_setup_completed(session, True)

    str(session.statements[0].compile(dialect=postgresql.dialect()))


@pytest.mark.asyncio
async def test_advanced_auth_flag_survives_it_too(monkeypatch):
    from backend.app.api.routes import auth

    monkeypatch.setattr(db_dialect, "is_postgres", lambda: True)
    session = _CapturingSession()

    await auth.set_advanced_auth_enabled(session, True)

    str(session.statements[0].compile(dialect=postgresql.dialect()))


def test_no_module_reaches_for_a_dialect_specific_insert_on_its_own():
    """The guard. A hand-rolled ON CONFLICT is invisible on SQLite and fatal on
    PostgreSQL, so the rule is enforced here rather than by memory."""
    offenders = []
    for path in BACKEND_APP.rglob("*.py"):
        if path.name == "db_dialect.py":
            continue  # the one place allowed to know about dialects
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in (
                "sqlalchemy.dialects.sqlite",
                "sqlalchemy.dialects.postgresql",
            ):
                for alias in node.names:
                    if alias.name == "insert":
                        offenders.append(f"{path.relative_to(BACKEND_APP)}:{node.lineno}")

    assert not offenders, (
        "These build an INSERT for one dialect and will be compiled by whichever "
        "backend is actually configured. Use core/db_dialect.upsert_setting, or "
        "add a branch there:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.asyncio
async def test_the_old_shape_really_does_break_on_postgresql():
    """Proof that the tests above are not vacuous.

    This is what the three call sites used to build. If SQLAlchemy ever makes
    the two dialects' OnConflictDoUpdate interchangeable, this test starts
    failing — and that is the signal to relax the guard, not to delete it.
    """
    from sqlalchemy import func
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    stmt = sqlite_insert(Settings).values(key="setup_completed", value="true")
    stmt = stmt.on_conflict_do_update(index_elements=["key"], set_={"value": "true", "updated_at": func.now()})

    with pytest.raises(AttributeError, match="constraint_target"):
        str(stmt.compile(dialect=postgresql.dialect()))
