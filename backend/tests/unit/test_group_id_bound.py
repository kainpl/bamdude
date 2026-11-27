"""N07 spec §B, T3: a deleted group's number is never handed out again (m263).

The bound covers every place a group number lives; the allocator is asked on its own
(an empty table whose counter already sits at the ceiling leaves no number); the
rebuild keeps rows, foreign keys and the deleted newest number reserved.
"""

from __future__ import annotations

import sqlite3

import pytest

from backend.app.core import group_id_bound, id_allocator
from backend.app.core.group_id_bound import CARRIERS, GroupIdBoundError
from backend.app.migrations import m263_group_id_never_reissued as m263
from backend.app.migrations._sqlite_autoincrement import SelfManagedFailure

MAX = id_allocator.MAX_ID

# FK carriers the models gained after m263: they appear only together with the AUTOINCREMENT
# counter, so a number they name is already behind it and the frozen registry never needs them
# (the reasoning ``group_id_bound`` gives for ``oidc_providers.group_mapping``).
ADDED_AFTER_COUNTER: set[tuple[str, str]] = {
    ("group_printers", "group_id"),  # N62: a group's picked printers (m270)
    ("group_locations", "group_id"),  # N62: a group's locations (m270)
}


def test_the_registry_names_every_model_column_that_references_a_group():
    from backend.app.core.database import Base, import_all_models

    import_all_models()
    referencing = {
        (table.name, fk.parent.name)
        for table in Base.metadata.tables.values()
        for fk in table.foreign_keys
        if fk.column.table.name == "groups" and fk.column.name == "id"
    }
    assert (referencing - ADDED_AFTER_COUNTER) | {("groups", "id")} == {(c.table, c.column) for c in CARRIERS}


def test_the_model_is_autoincrement():
    from backend.app.models.group import Group

    assert Group.__table__.dialect_options["sqlite"]["autoincrement"] is True


SCHEMA = {
    "table_pk": "CREATE TABLE groups (id INTEGER NOT NULL, name VARCHAR(100) NOT NULL, PRIMARY KEY (id))",
    "inline_pk": "CREATE TABLE groups (id INTEGER PRIMARY KEY, name VARCHAR(100) NOT NULL)",
}


def _database(tmp_path, shape="table_pk", *, default_group=True, applied=(1, 51)):
    conn = sqlite3.connect(tmp_path / f"{shape}.db", isolation_level=None)
    conn.execute(SCHEMA[shape])
    conn.execute("CREATE UNIQUE INDEX ix_groups_name ON groups (name)")
    conn.execute(
        "CREATE TABLE user_groups (user_id INTEGER NOT NULL, group_id INTEGER NOT NULL"
        " REFERENCES groups (id) ON DELETE CASCADE, PRIMARY KEY (user_id, group_id))"
    )
    column = ", default_group_id INTEGER REFERENCES groups (id) ON DELETE SET NULL" if default_group else ""
    conn.execute(f"CREATE TABLE oidc_providers (id INTEGER PRIMARY KEY{column})")
    conn.execute("CREATE TABLE telegram_chats (id INTEGER PRIMARY KEY, group_id INTEGER REFERENCES groups (id))")
    conn.execute("CREATE TABLE _migrations (version INTEGER PRIMARY KEY, name TEXT)")
    conn.executemany("INSERT INTO _migrations VALUES (?, ?)", [(v, f"m{v}") for v in applied])
    conn.executemany(
        "INSERT INTO groups (id, name) VALUES (?, ?)", [(1, "Administrators"), (2, "Viewers"), (3, "Team")]
    )
    return conn


def test_the_bound_takes_every_carrier(tmp_path):
    conn = _database(tmp_path)
    conn.execute("INSERT INTO user_groups VALUES (1, 7)")  # an orphan SQLite never cascaded
    conn.execute("INSERT INTO oidc_providers (id, default_group_id) VALUES (1, 9)")
    conn.execute("INSERT INTO telegram_chats (id, group_id) VALUES (1, 8)")
    assert group_id_bound.sqlite_bound(conn, {1, 51}) == 9


def test_values_outside_the_domain_or_lossy_do_not_count(tmp_path):
    conn = _database(tmp_path)
    conn.execute("INSERT INTO telegram_chats (id, group_id) VALUES (1, ?)", (MAX + 5,))
    conn.execute("INSERT INTO telegram_chats (id, group_id) VALUES (2, '6')")  # a text value int() takes
    conn.execute("INSERT INTO telegram_chats (id, group_id) VALUES (3, 4.5)")
    assert group_id_bound.sqlite_bound(conn, {1, 51}) == 6


def test_a_source_before_m051_has_no_default_group_column(tmp_path):
    conn = _database(tmp_path, default_group=False, applied=(1,))
    assert group_id_bound.sqlite_bound(conn, {1}) == 3
    with pytest.raises(GroupIdBoundError, match="migration 51"):
        group_id_bound.sqlite_bound(conn, {1, 51})


AUTOINCREMENT_DDL = "CREATE TABLE groups (id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR(100) NOT NULL)"


def _autoincrement_database(tmp_path, seq):
    """An already AUTOINCREMENT ``groups`` emptied of rows, its counter retained at ``seq``."""
    conn = sqlite3.connect(tmp_path / "autoincrement.db", isolation_level=None)
    conn.execute(AUTOINCREMENT_DDL)
    conn.execute("CREATE TABLE user_groups (user_id INTEGER, group_id INTEGER, PRIMARY KEY (user_id, group_id))")
    conn.execute("CREATE TABLE oidc_providers (id INTEGER PRIMARY KEY, default_group_id INTEGER)")
    conn.execute("CREATE TABLE telegram_chats (id INTEGER PRIMARY KEY, group_id INTEGER)")
    conn.execute("INSERT INTO groups (name) VALUES ('gone')")
    conn.execute("DELETE FROM groups")
    conn.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'groups'", (seq,))
    return conn


def test_the_counter_is_exhausted_by_a_carrier_or_by_the_allocator(tmp_path):
    with pytest.raises(GroupIdBoundError):
        group_id_bound.require_next_number(MAX, None)
    conn = _autoincrement_database(tmp_path, MAX)
    assert group_id_bound.sqlite_bound(conn, set()) is None  # no carrier remembers a number
    assert group_id_bound.sqlite_allocator(conn) == MAX
    with pytest.raises(GroupIdBoundError):
        group_id_bound.require_next_number(None, group_id_bound.sqlite_allocator(conn))


def test_an_empty_table_with_a_counter_at_the_ceiling_stops_the_migration(tmp_path):
    conn = _autoincrement_database(tmp_path, MAX)
    with pytest.raises(SelfManagedFailure):
        m263.upgrade_sqlite(conn)
    assert conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'groups'").fetchone()[0] == MAX


@pytest.mark.parametrize("shape", sorted(SCHEMA))
def test_the_rebuild_reserves_the_deleted_newest_number(tmp_path, shape):
    conn = _database(tmp_path, shape)
    conn.execute("INSERT INTO user_groups VALUES (1, 3)")
    conn.execute("DELETE FROM groups WHERE id = 3")  # the newest group goes; its membership stays (FK off)
    outcome = m263.upgrade_sqlite(conn)
    assert outcome.rebuilt and outcome.connection_safe
    ddl = conn.execute("SELECT sql FROM sqlite_schema WHERE name = 'groups'").fetchone()[0]
    assert "AUTOINCREMENT" in ddl.upper()
    assert conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'groups'").fetchone()[0] == 3
    conn.execute("INSERT INTO groups (name) VALUES ('After')")
    assert conn.execute("SELECT id FROM groups WHERE name = 'After'").fetchone()[0] == 4
    assert conn.execute("SELECT name FROM sqlite_schema WHERE name = 'ix_groups_name'").fetchone()
    fks = {row[2] for row in conn.execute("PRAGMA foreign_key_list(user_groups)")}
    assert fks == {"groups"}


def test_a_rerun_only_raises_the_counter(tmp_path):
    conn = _database(tmp_path)
    m263.upgrade_sqlite(conn)
    conn.execute("INSERT INTO telegram_chats (id, group_id) VALUES (1, 40)")
    again = m263.upgrade_sqlite(conn)
    assert not again.rebuilt
    assert conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'groups'").fetchone()[0] == 40
