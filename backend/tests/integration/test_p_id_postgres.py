"""P-ID on a disposable embedded PostgreSQL: m262's sequence arm, the SQLite import, the portable export.

T3 — the sequence is raised only upward, under a lock another session's INSERT waits on, only
for a plain sequence, and only while the allocator itself still has a number left (PID-S25);
T4 — the import keeps every number the source remembers, historical columns and the file's own
counter included, and a value PostgreSQL cannot hold refuses the import with the old target
intact (PID-S26); T5 — the portable copy keeps the counter PostgreSQL reached, and the round
trip back continues above it.
"""

import sqlite3
import uuid
from contextlib import closing

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, Text, text
from sqlalchemy.exc import DBAPIError

from backend.app import migrations
from backend.app.core import db_portable, pg_sequences
from backend.app.core.config import settings
from backend.app.core.database import Base
from backend.app.core.pg_sequences import SequenceError
from backend.app.core.printer_id_bound import MAX_PRINTER_ID
from backend.app.migrations import m262_printer_id_never_reissued as m262
from backend.tests.integration.test_embedded_postgres_live import live_settings  # noqa: F401
from backend.tests.integration.test_g01_a_postgres import test_engine  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def migration_dialect(test_engine, monkeypatch):
    monkeypatch.setattr(settings, "database_url", test_engine.url.render_as_string(hide_password=False))


def _neutral(data_type: str):
    from datetime import date, datetime

    if "timestamp" in data_type:
        return datetime(2026, 1, 1)
    if data_type == "date":
        return date(2026, 1, 1)
    if data_type in ("integer", "bigint", "smallint"):
        return 0
    if data_type in ("double precision", "real", "numeric"):
        return 0.0
    if data_type == "boolean":
        return False
    if data_type in ("json", "jsonb"):
        return "{}"
    if data_type == "bytea":
        return b"x"
    return f"x-{uuid.uuid4().hex[:12]}"


async def _fill(conn, table: str, *, returning: bool = True, **values):
    """Insert a row, giving every other NOT NULL column without a default a neutral value. FKs are off."""
    columns = (
        await conn.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = current_schema()"
                " AND table_name = :t AND is_nullable = 'NO' AND column_default IS NULL"
            ),
            {"t": table},
        )
    ).all()
    row = dict(values)
    for name, data_type in columns:
        if name not in row:
            row[name] = _neutral(data_type)
    names = ", ".join(f'"{n}"' for n in row)
    marks = ", ".join(f":{n}" for n in row)
    await conn.execute(text("SET LOCAL session_replication_role = replica"))
    tail = " RETURNING id" if returning else ""
    result = await conn.execute(text(f'INSERT INTO "{table}" ({names}) VALUES ({marks}){tail}'), row)
    return result.scalar() if returning else None


async def _printer(conn, **values) -> int:
    unique = str(uuid.uuid4())
    return await _fill(conn, "printers", serial_number=unique[:20], connector_identity=unique, **values)


async def _migrations_at(engine, *versions):
    await migrations._ensure_migrations_table(engine)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM _migrations"))
        for v in versions:
            await conn.execute(text("INSERT INTO _migrations (version, name) VALUES (:v, 'x')"), {"v": v})


async def _next_printer_id(engine) -> int:
    async with engine.begin() as conn:
        return await _printer(conn)


async def _upgrade(engine):
    async with engine.begin() as conn:
        await m262.upgrade(conn)


async def _sequence(engine):
    async with engine.begin() as conn:
        return await pg_sequences.sequence_state(conn, "printers")


# --- T3: m262 on PostgreSQL --------------------------------------------------------------------


async def test_m262_raises_the_sequence_above_every_remembered_number(test_engine):
    await _migrations_at(test_engine, *range(262))
    async with test_engine.begin() as conn:
        for _ in range(3):
            await _printer(conn)
        await conn.execute(text("DELETE FROM printers WHERE id = 3"))
        await _fill(conn, "printer_sensor_history", printer_id=20)
        await _fill(conn, "api_keys", printer_ids="[25]")
        await _fill(conn, "settings", key="default_printer_id", value="27")
        await _fill(conn, "ams_labels", ams_serial_number="p28a1")
        project = await _fill(conn, "projects", name="order")
        await _fill(
            conn,
            "order_working_plans",
            id=project,
            saved_entries='[{"entry_id": "e", "target": {"kind": "printer", "printer_id": 30}}]',
        )
    await _upgrade(test_engine)
    assert await _next_printer_id(test_engine) == 31
    await _upgrade(test_engine)  # nothing above 31 remembered now: left alone
    assert await _next_printer_id(test_engine) == 32


async def test_m262_leaves_a_sequence_already_past_the_bound(test_engine):
    await _migrations_at(test_engine, 261)
    async with test_engine.begin() as conn:
        for _ in range(5):
            await _printer(conn)
        await conn.execute(text("DELETE FROM printers WHERE id > 1"))
    before = await _sequence(test_engine)
    await _upgrade(test_engine)
    after = await _sequence(test_engine)
    assert (before.last_value, before.is_called) == (after.last_value, after.is_called) == (5, True)
    assert await _next_printer_id(test_engine) == 6


async def test_m262_on_an_untouched_sequence_without_printers_changes_nothing(test_engine):
    await _migrations_at(test_engine, 261)
    await _upgrade(test_engine)
    state = await _sequence(test_engine)
    assert (state.last_value, state.is_called) == (1, False)
    assert await _next_printer_id(test_engine) == 1


@pytest.mark.parametrize(
    ("alter", "message"),
    [("CACHE 5", "CACHE 1"), ("INCREMENT BY 2", "increment 2"), ("CYCLE", "NO CYCLE"), ("MAXVALUE 25", "exhausted")],
)
async def test_m262_stops_on_a_sequence_it_cannot_raise_safely(test_engine, alter, message):
    await _migrations_at(test_engine, 261)
    async with test_engine.begin() as conn:
        await _fill(conn, "cloud_link_printers", returning=False, printer_id=25)
        await conn.execute(text(f"ALTER SEQUENCE printers_id_seq {alter}"))
    with pytest.raises(SequenceError, match=message):
        await _upgrade(test_engine)


async def test_the_last_number_is_still_handed_out_before_the_sequence_is_called(test_engine):
    """``last_value = 2³¹ − 1`` with ``is_called = false``: that number is still next (PID-S25)."""
    await _migrations_at(test_engine, 261)
    async with test_engine.begin() as conn:
        await conn.execute(text("SELECT setval('printers_id_seq', :v, false)"), {"v": MAX_PRINTER_ID})
    await _upgrade(test_engine)
    assert await _next_printer_id(test_engine) == MAX_PRINTER_ID
    with pytest.raises(DBAPIError, match="maximum value"):
        await _next_printer_id(test_engine)


@pytest.mark.parametrize("with_rows", [False, True], ids=["empty-table", "rows"])
async def test_a_sequence_that_handed_out_the_last_number_stops_even_without_carriers(test_engine, with_rows):
    await _migrations_at(test_engine, 261)
    async with test_engine.begin() as conn:
        if with_rows:
            await _printer(conn)
        await conn.execute(text("SELECT setval('printers_id_seq', :v, true)"), {"v": MAX_PRINTER_ID})
    with pytest.raises(SequenceError, match="exhausted"):
        await _upgrade(test_engine)
    state = await _sequence(test_engine)
    assert (state.last_value, state.is_called) == (MAX_PRINTER_ID, True)


@pytest.mark.parametrize(("carrier", "passes"), [(99, True), (100, False)])
async def test_a_lower_seqmax_is_the_ceiling(test_engine, carrier, passes):
    await _migrations_at(test_engine, 261)
    async with test_engine.begin() as conn:
        await conn.execute(text("ALTER SEQUENCE printers_id_seq MAXVALUE 100"))
        await _fill(conn, "hms_muted_entries", printer_id=carrier)
    if passes:
        await _upgrade(test_engine)
        assert await _next_printer_id(test_engine) == 100
    else:
        with pytest.raises(SequenceError, match="ceiling 100"):
            await _upgrade(test_engine)


async def test_m262_refuses_an_exhausted_sequence_through_the_runner_and_writes_no_marker(test_engine, monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    await migrations._ensure_migrations_table(test_engine)
    async with test_engine.begin() as conn:
        await conn.execute(text("DELETE FROM _migrations"))
        for mig in migrations._discover_migrations():
            if mig["version"] < 262:
                await conn.execute(
                    text("INSERT INTO _migrations (version, name) VALUES (:v, :n)"),
                    {"v": mig["version"], "n": mig["name"]},
                )
        await conn.execute(text("SELECT setval('printers_id_seq', :v, true)"), {"v": MAX_PRINTER_ID})
    with pytest.raises(SequenceError, match="printers: the id sequence is exhausted"):
        await migrations._run_pending(test_engine, None)
    async with test_engine.begin() as conn:
        marker = (await conn.execute(text("SELECT count(*) FROM _migrations WHERE version = 262"))).scalar()
    assert marker == 0


async def test_m262_holds_inserts_off_and_a_rollback_never_lowers_the_sequence(test_engine):
    await _migrations_at(test_engine, 261)
    async with test_engine.begin() as conn:
        await _fill(conn, "hms_muted_entries", printer_id=75)
    holder = await test_engine.connect()
    try:
        transaction = await holder.begin()
        await m262.upgrade(holder)  # LOCK taken, setval done, not committed
        async with test_engine.connect() as other:
            with pytest.raises(DBAPIError, match="lock"):
                async with other.begin():
                    await other.execute(text("SET LOCAL lock_timeout = '300ms'"))
                    await _printer(other)
        await transaction.rollback()
    finally:
        await holder.close()
    state = await _sequence(test_engine)
    async with test_engine.begin() as conn:
        marker = (await conn.execute(text("SELECT count(*) FROM _migrations WHERE version = 262"))).scalar()
    assert (state.last_value, state.is_called, marker) == (75, True, 0)  # setval outlives its rollback, upward
    assert await _next_printer_id(test_engine) == 76


async def test_the_json_carriers_count_on_postgres_json(test_engine):
    await _migrations_at(test_engine, 261)
    async with test_engine.begin() as conn:
        await _fill(conn, "api_keys", printer_ids='{"33": true}')
        project = await _fill(conn, "projects", name="order", printer_resources='{"models": null, "printer_ids": [34]}')
        await _fill(conn, "order_working_plans", id=project, entries="[]")
        await _fill(
            conn,
            "order_plan_submissions",
            id="op",
            plan_id=project,
            request='{"parts": [{"entry": {"target": {"kind": "printer", "printer_id": 35}}, "count": 1}]}',
        )
    await _upgrade(test_engine)
    assert await _next_printer_id(test_engine) == 36


# --- T4: SQLite → PostgreSQL import ------------------------------------------------------------


def _narrow_model() -> MetaData:
    model = MetaData()
    Table("printers", model, Column("id", Integer, primary_key=True), Column("name", String))
    Table("print_state_recovery_actions", model, Column("id", Integer, primary_key=True), Column("printer_id", Integer))
    Table("settings", model, Column("key", String, primary_key=True), Column("value", Text))
    return model


def _source(path, *, version: int = 41, autoincrement: bool = False, script: str = "") -> bytes:
    pk = "id INTEGER PRIMARY KEY AUTOINCREMENT" if autoincrement else "id INTEGER NOT NULL, name TEXT, PRIMARY KEY (id)"
    extra = ", name TEXT" if autoincrement else ""
    with closing(sqlite3.connect(path)) as db:
        db.executescript(
            "CREATE TABLE _migrations(id INTEGER PRIMARY KEY, version INTEGER NOT NULL UNIQUE);"
            f"INSERT INTO _migrations VALUES (1, {version});"
            "CREATE TABLE settings(key VARCHAR PRIMARY KEY, value TEXT);"
            f"CREATE TABLE printers({pk}{extra});"
            "INSERT INTO printers (id, name) VALUES (5, 'p');"
            "CREATE TABLE print_state_recovery_actions(id INTEGER PRIMARY KEY, printer_id INTEGER);" + script
        )
    return path.read_bytes()


async def _next_id(engine, table: str) -> int:
    async with engine.begin() as conn:
        return (await conn.execute(text(f'INSERT INTO "{table}" DEFAULT VALUES RETURNING id'))).scalar()


async def test_import_keeps_a_number_only_a_historical_column_remembers(test_engine, tmp_path, caplog):
    source = tmp_path / "source.db"
    before = _source(
        source,
        script="CREATE TABLE kprofile_notes(id INTEGER PRIMARY KEY, printer_id INTEGER);"
        "INSERT INTO kprofile_notes VALUES (1, 150);",
    )
    caplog.set_level("INFO", logger="backend.app.core.db_portable")
    await db_portable.import_sqlite_to_postgres(test_engine, _narrow_model(), source)
    assert source.read_bytes() == before
    assert await _next_id(test_engine, "printers") == 151
    assert "Highest printer number the source remembers: 150" in caplog.text
    assert "Printer numbers up to 150 stay reserved" in caplog.text


@pytest.mark.parametrize(("counter", "historical", "expected"), [(40, 150, 151), (200, 150, 201)])
async def test_import_of_a_p_id_file_joins_its_counter_and_its_history(
    test_engine, tmp_path, counter, historical, expected
):
    source = tmp_path / "source.db"
    _source(
        source,
        version=262,
        autoincrement=True,
        script=(
            f"UPDATE sqlite_sequence SET seq = {counter} WHERE name = 'printers';"
            "CREATE TABLE kprofile_notes(id INTEGER PRIMARY KEY, printer_id INTEGER);"
            f"INSERT INTO kprofile_notes VALUES (1, {historical});"
        ),
    )
    await db_portable.import_sqlite_to_postgres(test_engine, _narrow_model(), source)
    assert await _next_id(test_engine, "printers") == expected


async def test_import_carries_an_emptied_printers_counter(test_engine, tmp_path):
    source = tmp_path / "source.db"
    _source(
        source, version=262, autoincrement=True, script="INSERT INTO printers (id) VALUES (60); DELETE FROM printers;"
    )
    await db_portable.import_sqlite_to_postgres(test_engine, _narrow_model(), source)
    assert await _next_id(test_engine, "printers") == 61


async def test_a_value_postgres_cannot_hold_refuses_the_import_and_keeps_the_target(test_engine, tmp_path):
    """PID-S26: the import is AS IS; a row PostgreSQL's ``integer`` cannot take stops the load."""
    good = tmp_path / "good.db"
    _source(good)
    await db_portable.import_sqlite_to_postgres(test_engine, _narrow_model(), good)
    bad = tmp_path / "bad.db"
    _source(bad, script=f"INSERT INTO print_state_recovery_actions VALUES (1, {MAX_PRINTER_ID + 1});")
    with pytest.raises(Exception):  # noqa: B017 — the driver's out-of-range error, whatever its class
        await db_portable.import_sqlite_to_postgres(test_engine, _narrow_model(), bad)
    async with test_engine.begin() as conn:  # the previous import stands
        assert (await conn.execute(text("SELECT id FROM printers"))).scalars().all() == [5]
        assert (await conn.execute(text("SELECT count(*) FROM print_state_recovery_actions"))).scalar() == 0
    assert await _next_id(test_engine, "printers") == 6


# --- T5: PostgreSQL → portable SQLite → back --------------------------------------------------


async def _full_schema_at_head(engine):
    await migrations._ensure_migrations_table(engine)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM _migrations"))
        for mig in migrations._discover_migrations():
            await conn.execute(
                text("INSERT INTO _migrations (version, name) VALUES (:v, :n)"), {"v": mig["version"], "n": mig["name"]}
            )


def _sqlite_insert_printer(db) -> int:
    row = {"name": "copy", "serial_number": "COPY0001", "ip_address": "192.0.2.9", "access_code": "00000000"}
    for _cid, name, declared, notnull, default, pk, _hidden in db.execute('PRAGMA table_xinfo("printers")'):
        if name in row or pk or not notnull or default is not None:
            continue
        declared = (declared or "").upper()
        row[name] = (
            "2026-01-01"
            if "DATE" in declared or "TIME" in declared
            else 0
            if any(t in declared for t in ("INT", "BOOL", "FLOAT", "NUMERIC"))
            else f"copy-{name}"
        )
    names = ", ".join(f'"{n}"' for n in row)
    return db.execute(
        f"INSERT INTO printers ({names}) VALUES ({', '.join('?' * len(row))})", list(row.values())
    ).lastrowid


async def test_the_portable_copy_keeps_the_printers_counter_both_ways(test_engine, tmp_path):
    await _full_schema_at_head(test_engine)
    async with test_engine.begin() as conn:
        for _ in range(4):
            await _printer(conn)
        await conn.execute(text("DELETE FROM printers WHERE id = 4"))
    copy = tmp_path / "portable.db"
    await db_portable.dump_to_sqlite(test_engine, Base.metadata, copy)

    with closing(sqlite3.connect(copy)) as db:
        assert db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'printers'").fetchone()[0] == 4
        assert "AUTOINCREMENT" in db.execute("SELECT sql FROM sqlite_schema WHERE name = 'printers'").fetchone()[0]
        db.execute("PRAGMA foreign_keys = OFF")
        assert _sqlite_insert_printer(db) == 5
        db.execute("DELETE FROM printers WHERE id = 5")
        db.commit()

    await db_portable.import_sqlite_to_postgres(test_engine, Base.metadata, copy)
    assert await _next_printer_id(test_engine) == 6
