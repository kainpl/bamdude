"""C047 spec §E: the frozen registry of the places a built-in spool number lives.

The bound must be at least every number any of them still holds, and nothing else:
a Spoolman number, a slot coordinate or a value of another type is not a spool.
"""

import sqlite3

import pytest

from backend.app.core import spool_id_bound
from backend.app.core.spool_id_bound import CARRIERS, SpoolIdBoundError, json_spool_ids, sqlite_bound

# FK carriers the models gained after C047 would go here: their numbers already come
# from an AUTOINCREMENT counter, so they never need to raise it (spec §E). None so far.
ADDED_AFTER_C047: set[tuple[str, str]] = set()
# Columns that hold spool numbers without a foreign key — the registry's own list.
WITHOUT_FK = {("print_usage_events", "spool_id"), ("label_jobs", "spool_id")}


def test_the_registry_names_every_model_column_that_holds_a_spool_number():
    from backend.app.core.database import Base, import_all_models

    import_all_models()
    referencing = {
        (table.name, fk.parent.name)
        for table in Base.metadata.tables.values()
        for fk in table.foreign_keys
        if fk.column.table.name == "spool" and fk.column.name == "id"
    }
    registry = {(c.table, c.column) for c in CARRIERS if c.kind == "int" and (c.table, c.column) != ("spool", "id")}
    assert registry - WITHOUT_FK == referencing - ADDED_AFTER_C047
    for table, column in WITHOUT_FK:
        assert column in Base.metadata.tables[table].c, (table, column)
    assert ("active_print_sessions", "spool_assignments") in {(c.table, c.column) for c in CARRIERS if c.kind == "json"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"0-1": 12, "1-3": 40}', [12, 40]),
        ({"0-0": 5}, [5]),
        (b'{"0-0": 6}', [6]),
        ('{"0-0": true, "0-1": "9", "0-2": null, "0-3": -4, "0-4": 0, "0-5": 1.5}', []),
        ("{}", []),
        ("not json", []),
        ("[1, 2]", []),
        (None, []),
    ],
)
def test_json_carrier_values(raw, expected):
    assert json_spool_ids(raw) == expected


def _db(level: int = 260, *, sessions_type: str = "JSON", drop: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        f"""
        CREATE TABLE _migrations (id INTEGER PRIMARY KEY, version INTEGER NOT NULL UNIQUE, name VARCHAR(100));
        CREATE TABLE spool (id INTEGER NOT NULL, material TEXT, PRIMARY KEY (id));
        CREATE TABLE spool_assignment (id INTEGER PRIMARY KEY, spool_id INTEGER REFERENCES spool (id));
        CREATE TABLE spool_k_profile (id INTEGER PRIMARY KEY, spool_id INTEGER NOT NULL REFERENCES spool (id));
        CREATE TABLE spool_usage_history (id INTEGER PRIMARY KEY, spool_id INTEGER REFERENCES spool (id));
        CREATE TABLE print_usage_events (id INTEGER PRIMARY KEY, spool_id INTEGER, spoolman_spool_id INTEGER);
        CREATE TABLE label_jobs (id INTEGER PRIMARY KEY, spool_id INTEGER);
        CREATE TABLE active_print_sessions (printer_id INTEGER PRIMARY KEY, spool_assignments {sessions_type});
        """
    )
    conn.executemany("INSERT INTO _migrations (version, name) VALUES (?, 'x')", [(v,) for v in range(level + 1)])
    conn.execute("INSERT INTO spool VALUES (3, 'PLA')")
    if drop:
        table, _, column = drop.partition(".")
        conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}" if column else f"DROP TABLE {table}")
    return conn


def _bound(conn) -> int | None:
    return sqlite_bound(conn, spool_id_bound.sqlite_applied_versions(conn))


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO spool_assignment (spool_id) VALUES (91)",
        "INSERT INTO spool_k_profile (spool_id) VALUES (91)",
        "INSERT INTO spool_usage_history (spool_id) VALUES (91)",
        "INSERT INTO print_usage_events (spool_id) VALUES (91)",
        "INSERT INTO label_jobs (spool_id) VALUES (91)",
        """INSERT INTO active_print_sessions VALUES (1, '{"0-1": 91, "0-2": 4}')""",
        "INSERT INTO spool VALUES (91, 'PETG')",
    ],
)
def test_each_carrier_raises_the_bound_on_its_own(statement):
    conn = _db()
    assert _bound(conn) == 3
    conn.execute(statement)
    assert _bound(conn) == 91


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO print_usage_events (spool_id, spoolman_spool_id) VALUES (NULL, 900)",  # Spoolman's numbers
        """INSERT INTO active_print_sessions VALUES (1, '{"900-900": 2}')""",  # slot coordinates are keys
        "INSERT INTO label_jobs (spool_id) VALUES ('x900')",  # text in an INTEGER column sorts above numbers
        "INSERT INTO spool_usage_history (spool_id) VALUES (-900)",
        """INSERT INTO active_print_sessions VALUES (2, '{"0-0": true}')""",
        "INSERT INTO active_print_sessions VALUES (3, 'garbage')",
    ],
)
def test_what_is_not_a_spool_number_does_not_count(statement):
    conn = _db()
    conn.execute(statement)
    assert _bound(conn) == 3


def test_a_carrier_its_level_does_not_guarantee_yet_is_skipped_when_missing():
    conn = _db(level=150, drop="label_jobs")  # label_jobs is m147's: guaranteed at 150
    with pytest.raises(SpoolIdBoundError, match="label_jobs"):
        _bound(conn)
    conn = _db(level=140, drop="label_jobs")  # before m147: absence is the history
    assert _bound(conn) == 3


def test_a_present_carrier_counts_below_its_guaranteed_level():
    conn = _db(level=200)  # active_print_sessions is guaranteed only from m221 — create_all brought it earlier
    conn.execute("""INSERT INTO active_print_sessions VALUES (1, '{"0-0": 55}')""")
    assert _bound(conn) == 55


def test_a_missing_guaranteed_column_stops():
    conn = _db(level=260, drop="print_usage_events.spool_id")
    with pytest.raises(SpoolIdBoundError, match="print_usage_events.spool_id"):
        _bound(conn)


@pytest.mark.parametrize("sessions_type", ["TEXT", "VARCHAR(2000)", "JSON"])
def test_the_json_carrier_accepts_its_declared_sqlite_forms(sessions_type):
    conn = _db(sessions_type=sessions_type)
    conn.execute("""INSERT INTO active_print_sessions VALUES (1, '{"0-0": 12}')""")
    assert _bound(conn) == 12


def test_a_carrier_of_an_unexpected_type_stops():
    conn = _db(sessions_type="BLOB")
    with pytest.raises(SpoolIdBoundError, match="unexpected type"):
        _bound(conn)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE spool (id TEXT PRIMARY KEY)")
    with pytest.raises(SpoolIdBoundError, match="spool.id"):
        sqlite_bound(conn, set())


def test_create_all_only_tables_may_be_missing_at_any_level():
    conn = _db(level=260, drop="spool_k_profile")
    assert _bound(conn) == 3
