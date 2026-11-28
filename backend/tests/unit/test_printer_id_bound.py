"""P-ID spec §B, T2: the frozen registry of the places a printer number lives.

The bound must be at least every number a current reader could still pick up, and
nothing else: a position, an AMS serial, a value outside 1..2³¹ − 1 or a value of
another class is not a printer number.
"""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from backend.app.core import printer_id_bound
from backend.app.core.printer_id_bound import (
    CARRIERS,
    MAX_PRINTER_ID,
    PrinterIdBoundError,
    ams_key_id,
    api_key_scope_ids,
    plan_entries_ids,
    plan_request_ids,
    require_next_number,
    resources_ids,
    setting_int_id,
    setting_list_ids,
    sqlite_allocator,
    sqlite_bound,
)
from backend.app.core.printer_scope import key_list

# Integer carriers without a foreign key — the registry's own list.
WITHOUT_FK = {
    ("auto_queue_returns", "printer_id"),
    ("auto_queue_returns", "old_queue_id"),
    ("auto_queue_unsent_returns", "printer_id"),
    ("auto_queue_unsent_returns", "old_queue_id"),
    ("print_state_recovery_actions", "printer_id"),
    ("print_state_recovery_actions", "queue_id"),
    ("user_notifications", "printer_id"),
}
# The two primary keys that ARE printer numbers (a printer's queue has the printer's own number).
PRIMARY_KEYS = {("printers", "id"), ("printer_queues", "id")}
# FK carriers the models gained after P-ID would go here: their numbers already come from
# an AUTOINCREMENT counter, so they never need to raise it (spec §B).
ADDED_AFTER_PID: set[tuple[str, str]] = {
    ("group_printers", "printer_id"),  # N62: a group's picked printers (m270)
    ("maintenance_history", "printer_id"),  # N09: the printer a reset was for (m274)
}


def _models():
    from backend.app.core.database import Base, import_all_models

    import_all_models()
    return Base.metadata


def test_the_registry_names_every_model_column_that_references_a_printer_or_its_queue():
    metadata = _models()
    referencing = {
        (table.name, fk.parent.name)
        for table in metadata.tables.values()
        for fk in table.foreign_keys
        if fk.column.table.name in ("printers", "printer_queues") and fk.column.name == "id"
    }
    # 35 on printers.id (group_printers since m270, maintenance_history since m274), 2 on printer_queues.id
    assert len(referencing) == 37
    current = {(c.table, c.column) for c in CARRIERS if c.kind == "int" and c.removed is None}
    assert current - WITHOUT_FK - PRIMARY_KEYS == referencing - ADDED_AFTER_PID


def test_every_current_carrier_is_a_model_column_and_every_historical_one_is_gone():
    metadata = _models()
    for carrier in CARRIERS:
        table = metadata.tables.get(carrier.table)
        if carrier.removed is None:
            assert table is not None and carrier.column in table.c, (carrier.table, carrier.column)
        else:
            assert table is None or carrier.column not in table.c, (carrier.table, carrier.column)
    keys = {c.key for c in CARRIERS if c.key}
    assert keys == {"default_printer_id", "virtual_printer_target_printer_id", "obico_enabled_printers"}


def test_the_notification_scopes_stay_outside_the_registry():
    """Owner 2026-11-25: on a farm every printer and chat is the operator's own (spec §B)."""
    named = {(c.table, c.column) for c in CARRIERS}
    assert not named & {
        ("telegram_chats", "printer_ids"),
        ("notification_providers", "printer_ids"),
        ("notification_providers", "printer_id"),
    }


# --- value forms ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "decoded",
    [[120, 3], {"120": True, "7": False}, "8", "123", [3.9, True], [], ["7", " 9 "]],
)
def test_a_readable_api_key_scope_is_exactly_what_the_gate_allows(decoded):
    gate = key_list(SimpleNamespace(printer_ids=decoded))
    assert sorted(api_key_scope_ids(json.dumps(decoded))) == sorted(v for v in gate if 1 <= v <= MAX_PRINTER_ID)


@pytest.mark.parametrize(
    ("decoded", "expected"),
    [
        ([0, 5], [5]),  # the gate allows 0, which is no printer
        ([None, 5], [5]),  # the gate refuses the whole scope: the parseable element stays reserved
        (["x", 5], [5]),
        ([[1], 5], [5]),
        (7, []),  # a number root: the gate cannot iterate it
        (None, []),  # NULL: every printer, no number
    ],
)
def test_an_unreadable_api_key_scope_reserves_a_conservative_superset(decoded, expected):
    assert api_key_scope_ids(json.dumps(decoded)) == expected


def test_an_api_key_scope_that_is_not_json_holds_no_number():
    assert api_key_scope_ids("not json") == []
    assert api_key_scope_ids(b"[4]") == [4]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("p3a0", 3),
        ("p3a-1", 3),
        ("p3a987654", 3),
        ("p12a128", 12),
        ("p03a0", None),
        ("p3a+1", None),
        ("p3a01", None),
        ("p0a1", None),
        ("00M09A350100001", None),  # a real AMS serial
        ("p3a0 ", None),
        (f"p{MAX_PRINTER_ID + 1}a0", None),
    ],
)
def test_the_ams_position_key(raw, expected):
    assert ams_key_id(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"models": null, "printer_ids": [4, "6", 7.0, true]}', [4, 6, 7, 1]),
        ('{"models": ["X1C"], "printer_ids": null}', []),
        ('{"printer_ids": []}', []),
        ("[4]", []),
        ("garbage", []),
        (None, []),
    ],
)
def test_order_printer_resources(raw, expected):
    assert resources_ids(raw) == expected


def test_order_plan_targets_in_entries_and_saved_entries():
    entries = [
        {"entry_id": "a", "target": {"kind": "printer", "printer_id": 9}},
        {"entry_id": "b", "target": {"kind": "auto"}},
        {"entry_id": "c", "target": {"kind": "auto", "printer_id": 11}},  # the schema refuses it; counted anyway
        {"entry_id": "d", "target": 5},
        {"entry_id": "e"},
        "not an entry",
    ]
    assert plan_entries_ids(json.dumps(entries)) == [9, 11]
    for malformed in ('{"target": {"printer_id": 3}}', "[1, 2]", "null", "garbage", None):
        assert plan_entries_ids(malformed) == []


def test_order_plan_submission_request_targets():
    request = {
        "parts": [
            {"entry": {"target": {"kind": "printer", "printer_id": 14}}, "count": 1},
            {"entry": {"target": {"kind": "auto"}}, "count": 2},
            {"entry": "x"},
            7,
        ]
    }
    assert plan_request_ids(json.dumps(request)) == [14]
    for malformed in ('{"parts": {}}', "[]", '{"entries": [{"target": {"printer_id": 3}}]}', None):
        assert plan_request_ids(malformed) == []


@pytest.mark.parametrize(("raw", "expected"), [("14", 14), ("", None), ("None", None), ("1.5", None), ("x", None)])
def test_an_integer_setting_is_read_as_int(raw, expected):
    assert setting_int_id(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"), [("[2, 5]", [2, 5]), ('["4", 5.0, true]', [4, 5, 1]), ("", []), ("{}", []), ("x", [])]
)
def test_the_obico_list(raw, expected):
    assert setting_list_ids(raw) == expected


# --- one SQLite database with every carrier --------------------------------------------------


def _db(
    level: int = 261, *, skip: set[str] = frozenset(), historical: bool = False, settings_value: str = "TEXT"
) -> sqlite3.Connection:
    """Every registry table, typeless columns: on SQLite a value's own class decides, not the declared type.
    ``settings`` is the exception: its schema is checked, so it has the baseline's own types."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE _migrations (id INTEGER PRIMARY KEY, version INTEGER NOT NULL UNIQUE, name TEXT)")
    conn.executemany("INSERT INTO _migrations (version, name) VALUES (?, 'x')", [(v,) for v in range(level + 1)])
    tables: dict[str, set[str]] = {}
    for carrier in CARRIERS:
        if carrier.removed is not None and not historical:
            continue
        tables.setdefault(carrier.table, set()).add(carrier.column)
        if carrier.key:
            tables[carrier.table].add("key")
    for table, columns in tables.items():
        if table in skip:
            continue
        if table == "settings":
            conn.execute(f"CREATE TABLE settings (id INTEGER PRIMARY KEY, key VARCHAR(100), value {settings_value})")
            continue
        conn.execute(f'CREATE TABLE "{table}" ({", ".join(f'"{c}"' for c in sorted(columns))})')
    conn.execute("INSERT INTO printers (id) VALUES (3)")
    return conn


def _bound(conn) -> int | None:
    return sqlite_bound(conn, printer_id_bound.sqlite_applied_versions(conn))


_WINNING = {
    "int": 91,
    "ams_key": "p91a0",
    "api_key_scope": "[91]",
    "resources": '{"models": null, "printer_ids": [91]}',
    "plan_entries": '[{"entry_id": "x", "target": {"kind": "printer", "printer_id": 91}}]',
    "plan_request": '{"parts": [{"entry": {"target": {"kind": "printer", "printer_id": 91}}, "count": 1}]}',
    "setting_int": "91",
    "setting_list": "[91]",
}


@pytest.mark.parametrize("carrier", CARRIERS, ids=lambda c: f"{c.table}.{c.column}{'.' + c.key if c.key else ''}")
def test_each_carrier_raises_the_bound_on_its_own(carrier):
    conn = _db(historical=True)
    assert _bound(conn) == 3
    if carrier.key:
        conn.execute(f'INSERT INTO "{carrier.table}" (key, value) VALUES (?, ?)', (carrier.key, _WINNING[carrier.kind]))
    else:
        conn.execute(f'INSERT INTO "{carrier.table}" ("{carrier.column}") VALUES (?)', (_WINNING[carrier.kind],))
    assert _bound(conn) == 91


@pytest.mark.parametrize(
    ("value", "expected"),
    [("007", 7), (7.0, 7), ("7", 7), (7.5, 3), ("x9", 3), (None, 3), (b"\x09", 3), (-9, 3), (0, 3)],
)
def test_the_class_of_a_value_in_an_integer_column_decides(value, expected):
    conn = _db()
    conn.execute("INSERT INTO printer_sensor_history (printer_id) VALUES (?)", (value,))
    assert _bound(conn) == expected


@pytest.mark.parametrize("value", [2**31, 9007199254740993, "99999999999999999999"])
def test_a_value_above_the_domain_is_not_reserved_and_stays(value):
    conn = _db()
    conn.execute("INSERT INTO printer_sensor_history (printer_id) VALUES (?)", (value,))
    conn.execute("INSERT INTO api_keys (printer_ids) VALUES (?)", (json.dumps([9007199254740993, 2**31]),))
    assert _bound(conn) == 3
    assert conn.execute("SELECT count(*) FROM printer_sensor_history").fetchone()[0] == 1


def test_an_orphan_at_the_top_of_the_domain_exhausts_it():
    conn = _db()
    conn.execute("INSERT INTO auto_queue_returns (printer_id) VALUES (?)", (MAX_PRINTER_ID,))
    assert _bound(conn) == MAX_PRINTER_ID
    with pytest.raises(PrinterIdBoundError, match="exhausted"):
        require_next_number(_bound(conn))
    require_next_number(MAX_PRINTER_ID - 1)  # the control: one number is still left


def test_the_allocator_counts_the_primary_key_and_the_counter_outside_the_domain():
    conn = _db()
    conn.execute("INSERT INTO printers (id) VALUES (3000000000)")
    assert _bound(conn) == 3 and sqlite_allocator(conn) == 3000000000
    conn = _db()
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT)")
    conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('printers', 40)")
    assert sqlite_allocator(conn) == 40


# --- presence ---------------------------------------------------------------------------------


def test_a_guaranteed_carrier_missing_after_its_migration_stops():
    conn = _db(level=230, skip={"active_print_sessions"})  # m221 creates it
    with pytest.raises(PrinterIdBoundError, match="active_print_sessions.printer_id"):
        _bound(conn)


def test_a_carrier_missing_before_its_migration_is_history():
    conn = _db(level=200, skip={"active_print_sessions", "order_working_plans", "order_plan_submissions"})
    assert _bound(conn) == 3


def test_a_skipped_marker_is_not_an_applied_migration():
    conn = _db(level=261, skip={"order_working_plans", "order_plan_submissions"})
    conn.execute("DELETE FROM _migrations WHERE version = 206")  # a gap is not m206
    assert _bound(conn) == 3


def test_a_present_carrier_counts_below_its_level():
    conn = _db(level=150)  # print_usage_events is m153's, created early by create_all
    conn.execute("INSERT INTO print_usage_events (printer_id) VALUES (44)")
    assert _bound(conn) == 44


def test_a_missing_guaranteed_column_stops():
    conn = _db(level=261)
    conn.execute("DROP TABLE projects")
    conn.execute("CREATE TABLE projects (name)")
    with pytest.raises(PrinterIdBoundError, match="projects.printer_resources"):
        _bound(conn)


def test_create_all_only_carriers_may_be_missing_at_any_level():
    conn = _db(level=261, skip={"ams_labels", "print_state_recovery_actions", "firmware_batch_items"})
    assert _bound(conn) == 3


# --- settings: the row is optional, the table's schema is checked (acceptance PID-A01) ----------


def test_a_valid_settings_table_without_the_rows_holds_no_number():
    assert _bound(_db()) == 3


@pytest.mark.parametrize("declared", ["TEXT", "VARCHAR(500)", "CLOB", "NVARCHAR(50)"])
def test_a_text_settings_value_is_read(declared):
    conn = _db(settings_value=declared)
    conn.execute("INSERT INTO settings (key, value) VALUES ('virtual_printer_target_printer_id', 41)")
    assert conn.execute("SELECT typeof(value) FROM settings").fetchone()[0] == "text"  # TEXT affinity
    assert _bound(conn) == 41


@pytest.mark.parametrize("declared", ["INTEGER", "", "BLOB", "NUMERIC", "REAL", "INT_TEXT"])
def test_a_settings_value_that_is_not_text_stops(declared):
    conn = _db(settings_value=declared)
    conn.execute("INSERT INTO settings (key, value) VALUES ('virtual_printer_target_printer_id', 41)")
    with pytest.raises(PrinterIdBoundError, match="settings.value: unexpected type"):
        _bound(conn)


@pytest.mark.parametrize("missing", ["key", "value"])
def test_a_settings_table_without_its_columns_stops_at_any_level(missing):
    for level in (0, 261):
        conn = _db(level=level, skip={"settings"})
        kept = "value TEXT" if missing == "key" else "key VARCHAR(100)"
        conn.execute(f"CREATE TABLE settings (id INTEGER PRIMARY KEY, {kept})")
        with pytest.raises(PrinterIdBoundError, match=f"settings.{missing}: column is missing"):
            _bound(conn)


def test_a_missing_settings_table_stops_once_the_baseline_is_applied():
    with pytest.raises(PrinterIdBoundError, match="settings.value: table is missing"):
        _bound(_db(level=261, skip={"settings"}))
    assert _bound(_db(level=0, skip={"settings"})) == 3


def test_an_integer_setting_handed_back_as_a_number_still_counts():
    assert setting_int_id(41) == 41 and setting_int_id(True) is None


# --- levels the acceptance review pinned (PID-A02) ---------------------------------------------


@pytest.mark.parametrize("table", ["spoolman_slot_assignments", "spoolman_k_profile"])
def test_the_spoolman_carriers_are_owed_from_m053(table):
    with pytest.raises(PrinterIdBoundError, match=f"{table}.printer_id"):
        _bound(_db(level=53, skip={table}))
    assert _bound(_db(level=52, skip={table})) == 3


def test_pipeline_jobs_is_owed_only_between_m102_and_m112():
    def at(versions: set[int], *, present: bool):
        conn = _db(level=0, historical=True, skip=set() if present else {"pipeline_jobs"})
        conn.execute("DELETE FROM _migrations")
        conn.executemany("INSERT INTO _migrations (version) VALUES (?)", [(v,) for v in sorted(versions)])
        return conn

    assert _bound(at(set(range(102)), present=False)) == 3  # before it appears
    with pytest.raises(PrinterIdBoundError, match="pipeline_jobs.assigned_printer_id"):
        _bound(at(set(range(112)), present=False))  # in its window
    assert _bound(at(set(range(262)), present=False)) == 3  # after m112 removed it
    late = at(set(range(262)), present=True)  # a column an old file still has keeps counting
    late.execute("INSERT INTO pipeline_jobs (assigned_printer_id) VALUES (70)")
    assert _bound(late) == 70


def test_a_historical_column_counts_while_an_old_file_still_has_it_and_its_absence_is_no_error():
    old = _db(level=1, historical=True)
    old.execute("INSERT INTO print_queue (printer_id) VALUES (60)")
    assert _bound(old) == 60
    late = _db(level=261, historical=True)  # the window has closed, the column is still there: it counts
    late.execute("INSERT INTO kprofile_notes (printer_id) VALUES (61)")
    assert _bound(late) == 61
    assert _bound(_db(level=261)) == 3  # and gone after its removal is simply history
