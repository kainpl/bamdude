"""C047 spec §A: the SQLite rebuild that gives ``spool`` AUTOINCREMENT, on plain sqlite3.

The helper owns its transaction and both pragmas; these tests drive it directly on a
connection with no transaction open, as the runner hands it one. The runner itself,
pending and DEBUG, on a pooled aiosqlite connection, is ``integration/test_m261_runner.py``.
"""

import sqlite3

import pytest

from backend.app.migrations._sqlite_autoincrement import SelfManagedFailure, ensure_autoincrement

TABLE_PK = """CREATE TABLE spool (
\tid INTEGER NOT NULL, \n\tlabel_identity VARCHAR(36), \n\tmaterial VARCHAR(50) NOT NULL, \n\tlocation_id INTEGER, \n\tnote VARCHAR(500) DEFAULT 'x' NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(location_id) REFERENCES locations (id)\n)"""
INLINE = 'CREATE TABLE "spool" ("id" INTEGER PRIMARY KEY, material TEXT NOT NULL, location_id INTEGER REFERENCES locations (id))'


def _db(ddl: str = TABLE_PK, *, foreign_keys: int = 0) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE locations (id INTEGER PRIMARY KEY)")
    conn.execute(ddl)
    conn.execute("CREATE INDEX ix_spool_location_id ON spool (location_id)")
    conn.execute("CREATE TABLE spool_usage_history (id INTEGER PRIMARY KEY, spool_id INTEGER REFERENCES spool (id))")
    conn.execute("CREATE TABLE audit (what TEXT)")
    conn.execute("CREATE TRIGGER spool_touch AFTER UPDATE ON spool BEGIN INSERT INTO audit VALUES ('own'); END")
    conn.execute(
        "CREATE TRIGGER history_names_spool AFTER INSERT ON spool_usage_history BEGIN "
        "INSERT INTO audit SELECT material FROM spool WHERE id = NEW.spool_id; END"
    )
    conn.execute("CREATE VIEW spool_names AS SELECT id, material FROM spool")
    conn.execute("INSERT INTO locations VALUES (1)")
    conn.executemany(
        "INSERT INTO spool (id, material, location_id) VALUES (?, ?, ?)", [(1, "PLA", 1), (2, "PETG", None)]
    )
    conn.execute("INSERT INTO spool_usage_history (spool_id) VALUES (7)")  # an orphan: spool 7 was deleted
    conn.commit()
    conn.execute(f"PRAGMA foreign_keys = {foreign_keys}")
    return conn


def _bound(conn) -> int | None:
    rows = conn.execute("SELECT MAX(spool_id) FROM spool_usage_history UNION ALL SELECT MAX(id) FROM spool").fetchall()
    values = [r[0] for r in rows if r[0] is not None]
    return max(values) if values else None


def _ddl(conn) -> str:
    return conn.execute("SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = 'spool'").fetchone()[0]


def _schema(conn) -> set[tuple]:
    return {
        row
        for row in conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'")
        if not (row[0] == "table" and row[1] == "spool")
    }


@pytest.mark.parametrize("ddl", [TABLE_PK, INLINE], ids=["create_all", "inline"])
@pytest.mark.parametrize("foreign_keys", [0, 1])
def test_rebuild_keeps_everything_and_never_reissues_a_number(ddl, foreign_keys):
    conn = _db(ddl, foreign_keys=foreign_keys)
    before_rows = conn.execute("SELECT * FROM spool ORDER BY id").fetchall()
    before_schema = _schema(conn)

    outcome = ensure_autoincrement(conn, "spool", _bound)

    assert outcome.rebuilt and outcome.connection_safe and outcome.sequence == 7
    assert "AUTOINCREMENT" in _ddl(conn).upper()
    assert conn.execute("SELECT * FROM spool ORDER BY id").fetchall() == before_rows
    assert _schema(conn) == before_schema  # own index and trigger recreated, view and other triggers untouched
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == foreign_keys
    assert not conn.in_transaction
    assert conn.execute("SELECT name FROM sqlite_schema WHERE name LIKE '_c047_%'").fetchall() == []
    # The orphan stays where it was; the violation set is what it was.
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == [("spool_usage_history", 1, "spool", 0)]
    # Triggers and the view work on the rebuilt table.
    conn.execute("UPDATE spool SET material = 'ABS' WHERE id = 2")
    conn.execute("INSERT INTO spool_usage_history (spool_id) VALUES (1)")
    assert conn.execute("SELECT what FROM audit ORDER BY rowid").fetchall() == [("own",), ("PLA",)]
    assert conn.execute("SELECT * FROM spool_names LIMIT 0").fetchall() == []
    # The next spool gets 8 — above the deleted 7 — and a deleted newest number never comes back.
    conn.execute("INSERT INTO spool (material) VALUES ('TPU')")
    assert conn.execute("SELECT MAX(id) FROM spool").fetchone()[0] == 8
    conn.execute("DELETE FROM spool WHERE id = 8")
    conn.execute("INSERT INTO spool (material) VALUES ('ASA')")
    assert conn.execute("SELECT MAX(id) FROM spool").fetchone()[0] == 9


def test_a_second_run_only_raises_the_counter():
    conn = _db()
    ensure_autoincrement(conn, "spool", _bound)
    ddl = _ddl(conn)
    outcome = ensure_autoincrement(conn, "spool", lambda _c: 40)
    assert not outcome.rebuilt and outcome.sequence == 40 and _ddl(conn) == ddl
    assert ensure_autoincrement(conn, "spool", lambda _c: 3).sequence == 40  # never down


def test_an_empty_database_starts_at_one():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE spool (id INTEGER NOT NULL, material TEXT, PRIMARY KEY (id))")
    outcome = ensure_autoincrement(conn, "spool", lambda _c: None)
    assert outcome.rebuilt and outcome.sequence is None
    conn.execute("INSERT INTO spool (material) VALUES ('PLA')")
    assert conn.execute("SELECT id FROM spool").fetchone()[0] == 1


@pytest.mark.parametrize("point", ["before_ddl", "after_copy", "after_drop", "at_bound", "before_commit"])
@pytest.mark.parametrize("foreign_keys", [0, 1])
def test_a_failure_at_any_point_changes_nothing(point, foreign_keys):
    conn = _db(foreign_keys=foreign_keys)
    ddl, rows, schema = _ddl(conn), conn.execute("SELECT * FROM spool").fetchall(), _schema(conn)

    def fault(at):
        if at == point:
            raise OSError(f"injected at {at}")

    with pytest.raises(SelfManagedFailure) as caught:
        ensure_autoincrement(conn, "spool", _bound, fault=fault)
    assert caught.value.connection_safe
    assert _ddl(conn) == ddl and _schema(conn) == schema
    assert conn.execute("SELECT * FROM spool").fetchall() == rows
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == foreign_keys
    assert conn.execute("SELECT name FROM sqlite_schema WHERE name LIKE '_c047_%'").fetchall() == []
    # And the next attempt succeeds.
    assert ensure_autoincrement(conn, "spool", _bound).rebuilt


@pytest.mark.parametrize(
    "ddl",
    [
        "CREATE TABLE spool (material TEXT, id INTEGER NOT NULL, PRIMARY KEY (id))",  # id not first
        "CREATE TABLE spool (id BIGINT NOT NULL, material TEXT, PRIMARY KEY (id))",  # not INTEGER
        "CREATE TABLE spool (id INTEGER NOT NULL, material TEXT, CONSTRAINT pk PRIMARY KEY (id))",
        "CREATE TABLE spool (id INTEGER, n INTEGER, material TEXT, PRIMARY KEY (id, n))",
        "CREATE TABLE spool (id INTEGER PRIMARY KEY DESC, material TEXT)",
        "CREATE TABLE spool (id INTEGER PRIMARY KEY, g INTEGER GENERATED ALWAYS AS (id * 2))",
        "CREATE TABLE spool (id INTEGER NOT NULL, material TEXT, PRIMARY KEY (id)) WITHOUT ROWID",
    ],
)
def test_an_unknown_shape_stops_before_any_change(ddl):
    conn = sqlite3.connect(":memory:")
    conn.execute(ddl)
    before = conn.execute("SELECT type, name, sql FROM sqlite_schema").fetchall()
    with pytest.raises(SelfManagedFailure) as caught:
        ensure_autoincrement(conn, "spool", lambda _c: 1)
    assert caught.value.connection_safe
    assert conn.execute("SELECT type, name, sql FROM sqlite_schema").fetchall() == before


def test_an_already_autoincrement_table_of_another_shape_stops():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE spool (material TEXT, id INTEGER PRIMARY KEY AUTOINCREMENT)")
    with pytest.raises(SelfManagedFailure):
        ensure_autoincrement(conn, "spool", lambda _c: 1)


def test_a_connection_with_a_transaction_open_is_refused_untouched():
    conn = _db()
    conn.execute("INSERT INTO locations VALUES (2)")  # sqlite3 opens a transaction for DML
    assert conn.in_transaction
    with pytest.raises(SelfManagedFailure) as caught:
        ensure_autoincrement(conn, "spool", _bound)
    assert caught.value.connection_safe and conn.in_transaction
    assert "AUTOINCREMENT" not in _ddl(conn).upper()


def test_an_unproven_rollback_marks_the_connection_unsafe(monkeypatch):
    from backend.app.migrations import _sqlite_autoincrement as helper

    conn = _db()
    real_run = helper._run

    def run(c, sql, params=()):
        if sql == "ROLLBACK":
            raise sqlite3.OperationalError("disk I/O error")
        return real_run(c, sql, params)

    monkeypatch.setattr(helper, "_run", run)

    def fault(at):
        if at == "after_drop":
            raise OSError("injected")

    with pytest.raises(SelfManagedFailure) as caught:
        ensure_autoincrement(conn, "spool", _bound, fault=fault)
    assert not caught.value.connection_safe
